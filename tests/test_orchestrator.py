import os
from copy import deepcopy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler.config import load_settings
from butler.chat import ChatError
from butler.agent import AgentReply
from butler.instance_lock import SingleInstance
from butler.handoff import RoleHandoffStore
from butler.orchestrator import (
    PLANNING_TOOLS,
    RoutedAgentSession,
    bounded_tool_payload,
    planning_tool_schemas,
    workspace_map,
)
from butler.tools import ToolResult


class OrchestratorTests(unittest.TestCase):
    def test_capability_roles_select_models_without_changing_tools(self):
        settings = load_settings()
        raw = deepcopy(settings.raw)
        raw["capability_roles"]["assistant"]["primary_model"] = "developer"
        raw["capability_roles"]["researcher"]["primary_model"] = "developer_qwopus"
        raw["capability_roles"]["developer"]["primary_model"] = "developer_qwopus"
        raw["capability_roles"]["heavy_brain"].update(
            {"enabled": True, "primary_model": "planner"}
        )
        raw["models"]["planner"]["enabled"] = True
        session = RoutedAgentSession(replace(settings, raw=raw))

        self.assertEqual(session._assistant_model(), "developer")
        self.assertEqual(session._research_model(), "developer_qwopus")
        self.assertEqual(session._execution_model(), "developer_qwopus")
        self.assertEqual(session._planning_model(), "planner")

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

    def test_planner_profile_uses_64k_context(self):
        self.assertEqual(load_settings().model("planner").context_size, 65_536)

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

    def test_news_request_uses_dedicated_research_route(self):
        session = RoutedAgentSession(load_settings())
        session.manager.is_current = Mock(return_value=True)
        session.research.run = Mock(return_value=AgentReply("Итог", ()))

        reply = session._ask_exclusive("Быстро найди последние новости о VR-интернете.")

        self.assertEqual(reply.text, "Итог")
        session.research.run.assert_called_once()

    def test_planner_exposes_only_read_tools(self):
        names = {
            schema["function"]["name"]
            for schema in planning_tool_schemas()
        }
        self.assertEqual(names, PLANNING_TOOLS)
        self.assertNotIn("write_workspace_file", names)
        self.assertNotIn("browser_interact", names)

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


if __name__ == "__main__":
    unittest.main()
