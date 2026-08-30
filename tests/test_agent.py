import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from butler.agent import (
    AgentSession,
    SYSTEM_PROMPT,
    _SafeFinalTextStream,
    bounded_result_payload,
    is_raw_tool_markup,
    status_for_tool,
)
from butler.chat import ChatError
from butler.tasking import TaskCancelled
from butler.tools import ToolResult


def tool_call_response(index: int) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {
                                "name": "browser_search",
                                "arguments": json.dumps({"query": f"VR news {index}"}),
                            },
                        }
                    ],
                }
            }
        ]
    }


class AgentTests(unittest.TestCase):
    @patch("butler.agent.diagnostic_event")
    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_tool_cancellation_is_a_terminal_lifecycle_state(
        self, complete, executor_class, diagnostic_event
    ):
        complete.return_value = tool_call_response(1)
        executor_class.return_value.execute.side_effect = TaskCancelled(
            "остановлено"
        )
        settings = SimpleNamespace(
            raw={"memory": {"compression_enabled": False}}
        )

        with self.assertRaises(TaskCancelled):
            AgentSession(settings).ask("Останови инструмент")

        tool_states = [
            call.kwargs["tool_state"]
            for call in diagnostic_event.call_args_list
            if len(call.args) >= 3 and call.args[2] == "tool_state_changed"
        ]
        self.assertEqual(tool_states, ["PLANNED", "STARTED", "CANCELLED"])

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_existing_task_security_context_is_not_reset(self, complete, executor_class):
        complete.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Готово."}}]
        }
        settings = SimpleNamespace(raw={"memory": {"compression_enabled": False}})
        session = AgentSession(settings)

        session.ask("Продолжи текущую задачу", reset_tool_state=False)

        executor_class.return_value.begin_task.assert_not_called()

    @patch("butler.agent.ToolExecutor")
    def test_context_snapshot_is_bounded_and_excludes_tool_transcript(self, _executor):
        settings = SimpleNamespace(raw={"memory": {"persistent": False}})
        session = AgentSession(settings)
        session.messages.extend(
            [
                {"role": "user", "content": "Обсудили голосовую активацию."},
                {
                    "role": "assistant",
                    "content": "Проверяю микрофон.",
                    "tool_calls": [{"id": "hidden"}],
                },
                {"role": "tool", "content": "Секретный технический вывод"},
                {"role": "assistant", "content": "Нужно проверить JBL позже."},
            ]
        )

        snapshot = session.context_snapshot(max_chars=1_000)

        self.assertIn("голосовую активацию", snapshot)
        self.assertIn("JBL", snapshot)
        self.assertNotIn("Секретный", snapshot)
        self.assertNotIn("Проверяю микрофон", snapshot)

    @patch("butler.agent.ToolExecutor")
    def test_live_memory_replaces_generated_answer_with_spoken_prefix(self, _executor):
        settings = SimpleNamespace(raw={"memory": {"persistent": False}})
        session = AgentSession(settings)
        session.record_exchange("Продолжай", "Первая. Вторая. Третья.")

        committed = session.commit_spoken_reply(
            "Первая. Вторая. Третья.", "Первая."
        )

        self.assertTrue(committed)
        self.assertEqual(session.messages[-1]["content"], "Первая.")

    @patch("butler.agent.ToolExecutor")
    def test_live_memory_records_spoken_prefix_after_cancelled_generation(self, _executor):
        settings = SimpleNamespace(raw={"memory": {"persistent": False}})
        session = AgentSession(settings)
        session.messages.append({"role": "user", "content": "Продолжай"})

        committed = session.commit_spoken_reply("Черновик ответа", "Успела сказать.")

        self.assertTrue(committed)
        self.assertEqual(
            session.messages[-1],
            {"role": "assistant", "content": "Успела сказать."},
        )

    @patch("butler.agent.ToolExecutor")
    def test_live_memory_refuses_to_rewrite_unrelated_answer(self, _executor):
        settings = SimpleNamespace(raw={"memory": {"persistent": False}})
        session = AgentSession(settings)
        session.record_exchange("Вопрос", "Существующий ответ.")

        committed = session.commit_spoken_reply("Другой ответ.", "Часть.")

        self.assertFalse(committed)
        self.assertEqual(session.messages[-1]["content"], "Существующий ответ.")

    def test_raw_tool_markup_is_never_a_spoken_answer(self):
        self.assertTrue(is_raw_tool_markup("<|tool_call>call:test{}<tool_call|>"))
        self.assertTrue(is_raw_tool_markup("  <tool_call>call:test{}"))
        self.assertTrue(
            is_raw_tool_markup("Ищу файл.<tool_call>search_workspace</tool_call>")
        )
        self.assertFalse(is_raw_tool_markup("Инструмент выполнил проверку."))

    def test_final_text_stream_holds_and_blocks_split_tool_prefix(self):
        spoken = []
        stream = _SafeFinalTextStream(spoken.append)

        stream.feed("  <|tool_")
        self.assertEqual(spoken, [])
        stream.feed("call>call:test{}")
        stream.finish("<|tool_call>call:test{}")

        self.assertEqual(spoken, [])
        self.assertTrue(stream.blocked)

        spoken = []
        stream = _SafeFinalTextStream(spoken.append)
        stream.feed("Промежуточный текст.<tool_")
        stream.feed("call>search_workspace")
        self.assertEqual(spoken, ["Промежуточный текст."])
        self.assertTrue(stream.blocked)

    def test_final_text_stream_releases_normal_prefix_without_duplication(self):
        spoken = []
        stream = _SafeFinalTextStream(spoken.append)

        stream.feed("Хорошо")
        stream.feed(", выполняю.")
        stream.finish("Хорошо, выполняю.")

        self.assertEqual(spoken, ["Хорошо", ", выполняю."])

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_raw_tool_markup_gets_one_plain_answer_retry(self, complete, executor_class):
        executor_class.return_value.execute.return_value = ToolResult(True, "ok", "готово")
        complete.side_effect = [
            tool_call_response(1),
            {"choices": [{"message": {"role": "assistant", "content": "<|tool_call>call:test{}"}}]},
            {"choices": [{"message": {"role": "assistant", "content": "Проверка готова."}}]},
        ]
        settings = SimpleNamespace(raw={"memory": {"compression_enabled": False}})

        reply = AgentSession(settings).ask("Тест", max_steps=1)

        self.assertEqual(reply.text, "Проверка готова.")
        self.assertIsNone(complete.call_args_list[1].kwargs["tools"])
        self.assertIsNone(complete.call_args_list[2].kwargs["tools"])

        complete.reset_mock()
        complete.side_effect = [
            tool_call_response(1),
            {"choices": [{"message": {"role": "assistant", "content": "<|tool_call>call:test{}"}}]},
            {"choices": [{"message": {"role": "assistant", "content": "<|tool_call>call:test{}"}}]},
        ]
        with self.assertRaisesRegex(
            ChatError, "Успешных изменений файлов: 0.*сохранённого состояния"
        ):
            AgentSession(settings).ask("Тест", max_steps=1)

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_raw_markup_after_spoken_prefix_is_not_retried_as_second_answer(
        self, complete, executor_class
    ):
        executor_class.return_value.execute.return_value = ToolResult(True, "ok", "готово")
        spoken = []

        def malformed_final(*_args, **kwargs):
            callback = kwargs.get("on_content_delta")
            callback("Начала. <tool_call>search_workspace</tool_call>")
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Начала. <tool_call>search_workspace</tool_call>",
                        }
                    }
                ]
            }

        responses = iter([tool_call_response(1), malformed_final])

        def respond(*args, **kwargs):
            value = next(responses)
            return value(*args, **kwargs) if callable(value) else value

        complete.side_effect = respond
        settings = SimpleNamespace(raw={"memory": {"compression_enabled": False}})

        with self.assertRaises(ChatError):
            AgentSession(settings).ask("Тест", max_steps=1, on_final_delta=spoken.append)

        self.assertEqual(complete.call_count, 2)
        self.assertEqual(spoken, ["Начала. "])

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_total_tool_call_limit_forces_final_answer(self, complete, executor_class):
        executor_class.return_value.execute.return_value = ToolResult(True, "ok", "готово")
        first = tool_call_response(1)
        first["choices"][0]["message"]["tool_calls"].extend(
            [
                tool_call_response(2)["choices"][0]["message"]["tool_calls"][0],
                tool_call_response(3)["choices"][0]["message"]["tool_calls"][0],
            ]
        )
        complete.side_effect = [
            first,
            {"choices": [{"message": {"role": "assistant", "content": "Итог."}}]},
        ]
        settings = SimpleNamespace(
            raw={
                "memory": {"compression_enabled": False},
                "agent": {"max_tool_calls_total": 2},
            }
        )

        reply = AgentSession(settings).ask("Тест", max_steps=8)

        self.assertEqual(reply.text, "Итог.")
        self.assertEqual(executor_class.return_value.execute.call_count, 2)
        self.assertEqual(reply.tool_events[-1].result.status, "tool_limit")
        self.assertIsNone(complete.call_args_list[-1].kwargs["tools"])

    def test_tool_result_payload_is_bounded(self):
        payload = bounded_result_payload(
            ToolResult(True, "ok", "прочитано", {"content": "x" * 20_000}),
            4_000,
        )
        self.assertLess(len(payload), 4_200)
        self.assertIn("результат сокращён", payload)

    def test_system_prompt_mentions_accessibility_and_confirmation(self):
        self.assertIn("слабовидящий", SYSTEM_PROMPT)
        self.assertIn("подтверждение", SYSTEM_PROMPT)
        self.assertIn("недоверенными", SYSTEM_PROMPT)

    def test_status_labels_are_human_readable(self):
        self.assertEqual(status_for_tool("browser_search"), "Ищу")
        self.assertEqual(status_for_tool("windows_active_window"), "Проверяю")
        self.assertEqual(status_for_tool("write_workspace_file"), "Выполняю")

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_conversation_only_uses_small_prompt_without_tools(
        self, complete, _executor_class
    ):
        complete.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Я Ксения."}}]
        }
        settings = SimpleNamespace(
            raw={
                "memory": {"compression_enabled": False},
                "agent": {
                    "conversation_history_messages": 4,
                    "conversation_max_tokens": 512,
                },
            }
        )
        session = AgentSession(settings)
        session.messages.extend(
            {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
            for index in range(20)
        )

        deltas = []
        reply = session.ask(
            "Как тебя зовут?",
            conversation_only=True,
            on_final_delta=deltas.append,
        )

        self.assertEqual(reply.text, "Я Ксения.")
        self.assertIsNone(complete.call_args.kwargs["tools"])
        self.assertEqual(complete.call_args.kwargs["max_tokens"], 512)
        self.assertIsNotNone(complete.call_args.kwargs["on_content_delta"])
        self.assertEqual(deltas, ["Я Ксения."])
        self.assertLessEqual(len(complete.call_args.args[1]), 5)

    @patch("butler.agent.ToolExecutor")
    def test_conversation_prompt_keeps_compressed_memory(self, _executor_class):
        settings = SimpleNamespace(
            raw={
                "memory": {"persistent": False},
                "agent": {"conversation_history_messages": 4},
            }
        )
        session = AgentSession(settings)
        session.messages.extend(
            [
                {
                    "role": "system",
                    "content": "Сжатая память предыдущего диалога:\nАлександру нравится голос Ксения.",
                },
                {"role": "user", "content": "Продолжим."},
            ]
        )

        messages = session._conversation_request_messages()

        self.assertIn("Александру нравится голос Ксения", messages[0]["content"])
        self.assertEqual([item["role"] for item in messages], ["system", "user"])

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_tool_limit_is_followed_by_final_answer(self, complete, executor_class):
        executor_class.return_value.execute.return_value = ToolResult(
            True, "ok", "Поиск выполнен", {"text": "результат"}
        )
        complete.side_effect = [
            tool_call_response(1),
            tool_call_response(2),
            tool_call_response(3),
            tool_call_response(4),
            {"choices": [{"message": {"role": "assistant", "content": "Итог."}}]},
        ]

        settings = SimpleNamespace(raw={"memory": {"compression_enabled": False}})
        reply = AgentSession(settings).ask("Найди новости", max_steps=4)

        self.assertEqual(reply.text, "Итог.")
        self.assertEqual(len(reply.tool_events), 4)
        self.assertEqual(complete.call_count, 5)
        self.assertIsNone(complete.call_args_list[-1].kwargs["tools"])

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.count_chat_tokens", return_value=50000)
    @patch("butler.agent.complete_chat")
    def test_context_compression_reports_status(
        self, complete, _count_tokens, _executor_class
    ):
        complete.side_effect = [
            {"choices": [{"message": {"content": "Краткая память."}}]},
            {"choices": [{"message": {"content": "Итоговый ответ."}}]},
        ]
        settings = SimpleNamespace(
            raw={
                "memory": {
                    "compression_enabled": True,
                    "compression_trigger_tokens": 48000,
                    "compression_summary_tokens": 100,
                    "keep_recent_messages": 4,
                }
            }
        )
        session = AgentSession(settings)
        session.messages.extend(
            [
                {"role": "user", "content": "старый вопрос 1"},
                {"role": "assistant", "content": "старый ответ 1"},
                {"role": "user", "content": "старый вопрос 2"},
                {"role": "assistant", "content": "старый ответ 2"},
                {"role": "user", "content": "недавний вопрос"},
                {"role": "assistant", "content": "недавний ответ"},
            ]
        )
        statuses = []

        reply = session.ask("новый вопрос", on_status=statuses.append)

        self.assertEqual(reply.text, "Итоговый ответ.")
        self.assertIn("Сжимаю контекст", statuses)
        self.assertIn("Контекст сжат", statuses)
        self.assertIn("Сжатая память", session.messages[1]["content"])

    @patch("butler.agent.diagnostic_event")
    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_confirmation_handler_retries_tool_as_confirmed(
        self, complete, executor_class, diagnostic_event
    ):
        executor = executor_class.return_value
        executor.execute.side_effect = [
            ToolResult(False, "confirmation_required", "Нужно подтверждение"),
            ToolResult(True, "ok", "Выполнено"),
        ]
        response = tool_call_response(1)
        response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = "write_workspace_file"
        complete.side_effect = [
            response,
            {"choices": [{"message": {"role": "assistant", "content": "Готово."}}]},
        ]
        settings = SimpleNamespace(raw={"memory": {"compression_enabled": False}})
        confirmations = []
        reply = AgentSession(settings).ask(
            "Запиши файл",
            on_confirmation=lambda name, arguments, message: confirmations.append(name) or True,
        )
        self.assertEqual(reply.tool_events[0].result.status, "ok")
        self.assertEqual(confirmations, ["write_workspace_file"])
        self.assertTrue(executor.execute.call_args_list[-1].kwargs["confirmed"])
        tool_states = [
            call.kwargs["tool_state"]
            for call in diagnostic_event.call_args_list
            if len(call.args) >= 3 and call.args[2] == "tool_state_changed"
        ]
        self.assertEqual(
            tool_states,
            ["PLANNED", "STARTED", "APPROVED", "STARTED", "COMPLETED"],
        )

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_one_approval_covers_related_changes_in_same_task(self, complete, executor_class):
        executor = executor_class.return_value

        def execute(_name, _arguments, *, confirmed=False):
            if not confirmed:
                return ToolResult(False, "confirmation_required", "Нужно подтверждение")
            return ToolResult(True, "ok", "Выполнено")

        executor.execute.side_effect = execute
        first = tool_call_response(1)
        first_call = first["choices"][0]["message"]["tool_calls"][0]
        first_call["function"] = {
            "name": "write_workspace_file",
            "arguments": json.dumps({"path": "a.txt", "content": "a"}),
        }
        second = tool_call_response(2)
        second_call = second["choices"][0]["message"]["tool_calls"][0]
        second_call["function"] = {
            "name": "replace_in_workspace_file",
            "arguments": json.dumps(
                {"path": "a.txt", "old_text": "a", "new_text": "b"}
            ),
        }
        complete.side_effect = [
            first,
            second,
            {"choices": [{"message": {"role": "assistant", "content": "Готово."}}]},
        ]
        settings = SimpleNamespace(raw={"memory": {"compression_enabled": False}})
        confirmations = []

        reply = AgentSession(settings).ask(
            "Измени проект",
            on_confirmation=lambda name, _arguments, _message: confirmations.append(name) or True,
        )

        self.assertEqual(reply.text, "Готово.")
        self.assertEqual(confirmations, ["write_workspace_file"])
        self.assertTrue(executor.execute.call_args_list[-1].kwargs["confirmed"])

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_confirmation_request_limit_stops_prompt_flood(self, complete, executor_class):
        executor = executor_class.return_value

        def execute(_name, _arguments, *, confirmed=False):
            return (
                ToolResult(True, "ok", "Выполнено")
                if confirmed
                else ToolResult(False, "confirmation_required", "Нужно подтверждение")
            )

        executor.execute.side_effect = execute
        response = tool_call_response(1)
        response["choices"][0]["message"]["tool_calls"] = []
        for index in range(3):
            response["choices"][0]["message"]["tool_calls"].append(
                {
                    "id": f"delete-{index}",
                    "type": "function",
                    "function": {
                        "name": "delete_workspace_file",
                        "arguments": json.dumps({"path": f"file-{index}.txt"}),
                    },
                }
            )
        complete.side_effect = [
            response,
            {"choices": [{"message": {"role": "assistant", "content": "Остановлено безопасно."}}]},
        ]
        settings = SimpleNamespace(
            raw={
                "memory": {"compression_enabled": False},
                "agent": {
                    "max_tool_calls_total": 16,
                    "max_confirmation_requests": 2,
                },
            }
        )
        confirmations = []

        reply = AgentSession(settings).ask(
            "Удали три файла",
            on_confirmation=lambda name, _arguments, _message: confirmations.append(name) or True,
        )

        self.assertEqual(len(confirmations), 2)
        self.assertEqual(reply.tool_events[-1].result.status, "confirmation_limit")
        self.assertIsNone(complete.call_args_list[-1].kwargs["tools"])


if __name__ == "__main__":
    unittest.main()
