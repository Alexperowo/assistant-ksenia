import os
from copy import deepcopy
from contextlib import nullcontext
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler.config import load_settings
from butler.chat import ChatError
from butler.agent import AgentReply
from butler.diagnostics import current_trace_fields
from butler.instance_lock import SingleInstance
from butler.handoff import RoleHandoffStore
from butler.orchestrator import (
    PLANNING_TOOLS,
    RoutedAgentSession,
    bounded_tool_payload,
    planning_tool_schemas,
    workspace_map,
)
from butler.tasking import TaskCancelled
from butler.tools import ToolResult


class OrchestratorTests(unittest.TestCase):
    def test_public_request_restores_task_trace_for_entire_route(self):
        session = RoutedAgentSession(load_settings())
        observed = []

        def routed(*_args, **_kwargs):
            observed.append(current_trace_fields())
            return AgentReply("Готово", ())

        session._ask_traced = Mock(side_effect=routed)
        control = SimpleNamespace(task_id="task-route", trace_id="trace-route")

        reply = session.ask("Проверка", control=control)

        self.assertEqual(reply.text, "Готово")
        self.assertEqual(
            observed,
            [{"trace_id": "trace-route", "task_id": "task-route"}],
        )

    def test_capability_roles_select_models_without_changing_tools(self):
        settings = load_settings()
        raw = deepcopy(settings.raw)
        raw["capability_roles"]["assistant"]["primary_model"] = "generalist"
        raw["capability_roles"]["researcher"]["primary_model"] = "reasoning"
        raw["capability_roles"]["developer"]["primary_model"] = "generalist"
        raw["capability_roles"]["heavy_brain"].update(
            {"enabled": True, "primary_model": "reasoning"}
        )
        session = RoutedAgentSession(replace(settings, raw=raw))

        self.assertEqual(session._assistant_model(), "generalist")
        self.assertEqual(session._research_model(), "reasoning")
        self.assertEqual(session._execution_model(), "generalist")
        self.assertEqual(session._planning_model(), "reasoning")

    def test_durable_task_records_cross_role_request_and_result(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(load_settings(), runtime_dir=Path(directory))
            session = RoutedAgentSession(settings)
            control = SimpleNamespace(task_id="a" * 32)

            reply = session._ask_exclusive("Какое сегодня число?", control=control)

            items = RoleHandoffStore(settings.runtime_dir).list_task(control.task_id)
            self.assertIn("Сегодня", reply.text)
            self.assertEqual([item.kind for item in items], ["request", "result"])
            self.assertEqual([item.role for item in items], ["assistant", "assistant"])

    @unittest.skipUnless(os.name == "nt", "Windows named mutex test")
    def test_second_agent_task_is_rejected_before_model_work(self):
        settings = load_settings()
        session = RoutedAgentSession(settings)
        with SingleInstance(settings.root, "agent-task") as acquired:
            self.assertTrue(acquired)
            with self.assertRaisesRegex(ChatError, "уже выполняет"):
                session.ask("Проверка конкурентного запуска")

    def test_reasoning_profile_uses_96k_context(self):
        self.assertEqual(load_settings().model("reasoning").context_size, 98_304)

    def test_short_conversation_uses_direct_route_but_actions_do_not(self):
        session = RoutedAgentSession(load_settings())
        self.assertTrue(
            session._is_direct_conversation(
                "Скажи, как тебя зовут, и назови текущую дату"
            )
        )
        self.assertFalse(
            session._is_direct_conversation(
                "Привет, найди реальные цены в двух магазинах"
            )
        )

    def test_date_fast_path_does_not_start_model(self):
        session = RoutedAgentSession(load_settings())
        session.manager.start = Mock()
        session.session.record_exchange = Mock()

        reply = session._ask_exclusive("Какое сегодня число?")

        self.assertIn("Сегодня", reply.text)
        session.manager.start.assert_not_called()
        session.session.record_exchange.assert_called_once()

    @patch("butler.orchestrator.ModelManager.for_role")
    def test_news_request_uses_dedicated_research_route(self, for_role):
        session = RoutedAgentSession(load_settings())
        research_manager = Mock()
        research_manager.is_current.return_value = True
        for_role.return_value = research_manager
        session.residency.activate_residents = Mock(return_value={})
        session.research.run = Mock(return_value=AgentReply("Итог", ()))

        reply = session._ask_exclusive("Быстро найди последние новости о VR-интернете.")

        self.assertEqual(reply.text, "Итог")
        for_role.assert_called_once_with(session.settings, "research_fast")
        session.residency.activate_residents.assert_called_once_with()
        research_manager.start.assert_not_called()
        session.research.run.assert_called_once()

    def test_primary_route_is_wrapped_in_residency_window(self):
        session = RoutedAgentSession(load_settings())
        session.residency.primary_window = Mock(return_value=nullcontext())
        session._run_primary_route = Mock(return_value=AgentReply("Готово", ()))

        reply = session._ask_exclusive("Выполни локальную задачу")

        self.assertEqual(reply.text, "Готово")
        session.residency.primary_window.assert_called_once_with()
        session._run_primary_route.assert_called_once()

    def test_planner_exposes_only_read_tools(self):
        names = {
            schema["function"]["name"]
            for schema in planning_tool_schemas()
        }
        self.assertEqual(names, PLANNING_TOOLS)
        self.assertNotIn("write_workspace_file", names)
        self.assertNotIn("browser_interact", names)

    @patch("butler.orchestrator.complete_chat")
    def test_planner_can_execute_enabled_rag_tool(self, complete_chat):
        original = load_settings()
        raw = deepcopy(original.raw)
        raw["rag"]["enabled"] = True
        settings = replace(original, raw=raw)
        complete_chat.side_effect = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "rag-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_project_knowledge",
                                        "arguments": '{"query":"граница разрешений"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "Факты."}}]},
            {"choices": [{"message": {"role": "assistant", "content": "План."}}]},
        ]
        session = RoutedAgentSession(settings)
        session.manager.switch = Mock()
        session.session.tools.execute = Mock(
            return_value=ToolResult(True, "ok", "Найдено", {"items": []})
        )

        plan = session._make_plan("Проведи аудит проекта", None)

        self.assertEqual(plan, "План.")
        session.session.tools.execute.assert_called_once_with(
            "search_project_knowledge",
            {"query": "граница разрешений"},
            confirmed=False,
        )

    @patch("butler.orchestrator.complete_chat")
    def test_planner_confirms_outbound_request_after_receiving_local_context(
        self, complete_chat
    ):
        complete_chat.side_effect = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "search-1",
                                    "type": "function",
                                    "function": {
                                        "name": "browser_search",
                                        "arguments": '{"query":"официальная документация"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "Факты."}}]},
            {"choices": [{"message": {"role": "assistant", "content": "План."}}]},
        ]
        session = RoutedAgentSession(load_settings())
        session.manager.switch = Mock()
        session.session.tools.mark_local_data_exposed = Mock()
        session.session.tools.execute = Mock(
            side_effect=[
                ToolResult(False, "confirmation_required", "Нужно подтверждение."),
                ToolResult(True, "ok", "Найдено", {"results": []}),
            ]
        )
        confirm = Mock(return_value=True)

        plan = session._make_plan(
            "Изучи проект и официальный источник",
            None,
            on_confirmation=confirm,
        )

        self.assertEqual(plan, "План.")
        session.session.tools.mark_local_data_exposed.assert_called_once_with()
        self.assertEqual(session.session.tools.execute.call_count, 2)
        self.assertFalse(session.session.tools.execute.call_args_list[0].kwargs["confirmed"])
        self.assertTrue(session.session.tools.execute.call_args_list[1].kwargs["confirmed"])
        confirm.assert_called_once()

    def test_planning_cancellation_does_not_start_execution_model(self):
        session = RoutedAgentSession(load_settings())
        session._planner_available = Mock(return_value=True)
        session._needs_plan = Mock(return_value=True)
        session._make_plan = Mock(side_effect=TaskCancelled("остановлено"))
        session.manager.start = Mock()

        with self.assertRaises(TaskCancelled):
            session._ask_exclusive("Проведи аудит проекта")

        session.manager.start.assert_not_called()

    def test_execution_preserves_planner_information_flow_state(self):
        session = RoutedAgentSession(load_settings())
        session._planner_available = Mock(return_value=True)
        session._needs_plan = Mock(return_value=True)
        session._make_plan = Mock(return_value="План.")
        session.manager.start = Mock()
        session.session.ask = Mock(return_value=AgentReply("Готово.", ()))

        session._ask_exclusive("Проведи аудит проекта")

        self.assertFalse(session.session.ask.call_args.kwargs["reset_tool_state"])

    def test_workspace_map_lists_files_and_skips_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            (workspace / "src").mkdir(parents=True)
            (workspace / "runtime").mkdir()
            (workspace / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
            (workspace / "runtime" / "secret.log").write_text("hidden", encoding="utf-8")
            settings = SimpleNamespace(
                root=root,
                raw={"developer": {"workspace_dir": "project"}},
            )
            result = workspace_map(settings)
            self.assertIn("src/app.py", result)
            self.assertNotIn("secret.log", result)

    def test_tool_payload_is_bounded(self):
        result = ToolResult(True, "ok", "прочитано", {"content": "x" * 500})
        payload = bounded_tool_payload(result, max_chars=100)
        self.assertIn("результат сокращён", payload)
        self.assertLess(len(payload), 250)

    @patch("butler.orchestrator.complete_chat")
    def test_planner_announces_research_and_plan(self, complete_chat):
        complete_chat.side_effect = [
            {"choices": [{"message": {"role": "assistant", "content": "Факты собраны."}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Короткий план."}}]},
        ]
        session = RoutedAgentSession(load_settings())
        session.manager.switch = Mock()
        statuses: list[str] = []

        plan = session._make_plan("Проверь документацию", statuses.append)

        self.assertEqual(plan, "Короткий план.")
        self.assertEqual(statuses, ["Планирую на усиленной модели", "Составляю план"])

    @patch("butler.orchestrator.complete_chat")
    def test_planner_inherits_task_wide_preconfirmation(self, complete_chat):
        complete_chat.side_effect = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "search-1",
                                    "type": "function",
                                    "function": {
                                        "name": "browser_search",
                                        "arguments": '{"query":"официальная документация"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "Факты."}}]},
            {"choices": [{"message": {"role": "assistant", "content": "План."}}]},
        ]
        session = RoutedAgentSession(load_settings())
        session.manager.switch = Mock()
        session.session.tools.execute = Mock(
            return_value=ToolResult(True, "ok", "Найдено", {"results": []})
        )

        plan = session._make_plan(
            "Изучи проект и официальный источник",
            None,
            confirmed=True,
        )

        self.assertEqual(plan, "План.")
        self.assertTrue(session.session.tools.execute.call_args.kwargs["confirmed"])


if __name__ == "__main__":
    unittest.main()
