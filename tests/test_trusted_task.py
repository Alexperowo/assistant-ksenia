from __future__ import annotations

import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler.agent import AgentReply, AgentSession
from butler.chat import ChatError
from butler.cli import _trusted_task_control, build_parser
from butler.config import load_settings
from butler.orchestrator import RoutedAgentSession
from butler.permissions import Decision, PermissionBroker
from butler.tools import ToolResult, tool_schemas
from butler.trusted_task import (
    TRUSTED_TASK_FINISHED,
    TRUSTED_TASK_MAX_TTL_SECONDS,
    TRUSTED_TASK_STARTED,
    TRUSTED_TASK_WARNING,
    TrustedTaskStore,
)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
        self.monotonic = 10_000.0

    def utc_now(self) -> datetime:
        return self.now

    def monotonic_now(self) -> float:
        return self.monotonic

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.monotonic += seconds


class TrustedTaskStoreTests(unittest.TestCase):
    def _store(self, root: Path, clock: _Clock | None = None) -> TrustedTaskStore:
        return TrustedTaskStore(
            root,
            utc_now=clock.utc_now if clock else None,
            monotonic_now=clock.monotonic_now if clock else None,
        )

    def test_grant_is_visible_then_consumed_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            armed = store.arm()

            self.assertEqual(store.status().grant_id, armed.grant_id)
            self.assertEqual(store.consume().grant_id, armed.grant_id)
            self.assertIsNone(store.consume())
            self.assertIsNone(store.status())

    def test_ttl_is_hard_capped_at_thirty_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            grant = store.arm(ttl_seconds=99_999)

            self.assertEqual(grant.ttl_seconds, TRUSTED_TASK_MAX_TTL_SECONDS)
            raw = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(raw["ttl_seconds"], TRUSTED_TASK_MAX_TTL_SECONDS)

    def test_expired_grant_fails_closed_and_consumer_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()
            store = self._store(Path(directory), clock)
            store.arm(ttl_seconds=10)
            clock.advance(11)

            self.assertIsNone(store.status())
            self.assertIsNone(store.consume())
            self.assertFalse(store.path.exists())

    def test_grant_from_previous_windows_boot_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = _Clock()
            store = self._store(Path(directory), clock)
            store.arm()
            clock.monotonic = 2.0

            self.assertIsNone(store.consume())
            self.assertFalse(store.path.exists())

    def test_malformed_state_fails_closed_and_consumer_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.path.parent.mkdir(parents=True)
            store.path.write_text('{"schema_version": 999}', encoding="utf-8")

            self.assertIsNone(store.status())
            self.assertIsNone(store.consume())
            self.assertFalse(store.path.exists())

    def test_non_finite_monotonic_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.arm()
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            payload["created_monotonic"] = float("nan")
            store.path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(store.consume())
            self.assertFalse(store.path.exists())

    def test_concurrent_consumers_have_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            self._store(runtime).arm()

            def consume_once(_index: int):
                return self._store(runtime).consume()

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(consume_once, range(16)))

            self.assertEqual(sum(result is not None for result in results), 1)

    def test_cancel_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(Path(directory))
            store.arm()

            self.assertTrue(store.cancel())
            self.assertFalse(store.cancel())


class TrustedTaskIntegrationTests(unittest.TestCase):
    def _session(self, directory: str) -> RoutedAgentSession:
        root = Path(directory)
        settings = replace(load_settings(), root=root, runtime_dir=root / "runtime")
        return RoutedAgentSession(settings)

    def test_next_routed_task_receives_task_wide_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(directory)
            session.trusted_tasks.arm()
            session._ask_exclusive = Mock(return_value=AgentReply("готово", ()))
            statuses: list[str] = []

            reply = session.ask("Выполни безопасную задачу", on_status=statuses.append)

            self.assertEqual(reply.text, "готово")
            self.assertTrue(session._ask_exclusive.call_args.kwargs["confirmed"])
            self.assertEqual(statuses, [TRUSTED_TASK_STARTED, TRUSTED_TASK_FINISHED])
            self.assertIsNone(session.trusted_tasks.status())

    def test_failed_task_still_consumes_grant_and_restores_normal_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(directory)
            session.trusted_tasks.arm()
            session._ask_exclusive = Mock(side_effect=ChatError("сбой"))
            statuses: list[str] = []

            with self.assertRaises(ChatError):
                session.ask("Задача со сбоем", on_status=statuses.append)

            self.assertEqual(statuses, [TRUSTED_TASK_STARTED, TRUSTED_TASK_FINISHED])
            self.assertIsNone(session.trusted_tasks.status())

    def test_explicit_internal_confirmation_does_not_consume_pending_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(directory)
            grant = session.trusted_tasks.arm()
            session._ask_exclusive = Mock(return_value=AgentReply("готово", ()))

            session.ask("Внутренне подтверждённая проверка", confirmed=True)

            self.assertEqual(session.trusted_tasks.status().grant_id, grant.grant_id)

    def test_model_cannot_activate_trust_and_hard_denies_remain(self):
        settings = load_settings()
        names = {item["function"]["name"] for item in tool_schemas(settings)}

        self.assertFalse(any("trust" in name or "довер" in name for name in names))
        self.assertEqual(
            PermissionBroker(settings).authorize("financial_action", confirmed=True).decision,
            Decision.DENY,
        )
        self.assertIn("секрет", TRUSTED_TASK_WARNING.casefold())
        self.assertIn("рабоч", TRUSTED_TASK_WARNING.casefold())
        self.assertIn("учётной записи windows", TRUSTED_TASK_WARNING.casefold())

    @patch("butler.agent.ToolExecutor")
    @patch("butler.agent.complete_chat")
    def test_task_wide_confirmation_skips_even_fresh_delete_prompt(
        self, complete_chat, executor_class
    ):
        executor = executor_class.return_value

        def execute(_name, _arguments, *, confirmed=False):
            return (
                ToolResult(True, "ok", "Выполнено")
                if confirmed
                else ToolResult(False, "confirmation_required", "Нужно подтверждение")
            )

        executor.execute.side_effect = execute
        complete_chat.side_effect = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "delete-1",
                                    "type": "function",
                                    "function": {
                                        "name": "delete_workspace_file",
                                        "arguments": json.dumps({"path": "temp.txt"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "Готово."}}]},
        ]
        confirmation = Mock(return_value=True)
        settings = SimpleNamespace(raw={"memory": {"compression_enabled": False}})

        reply = AgentSession(settings).ask(
            "Удали временный файл",
            confirmed=True,
            on_confirmation=confirmation,
        )

        self.assertEqual(reply.tool_events[0].result.status, "ok")
        self.assertTrue(executor.execute.call_args.kwargs["confirmed"])
        confirmation.assert_not_called()

    def test_cli_exposes_only_interactive_local_activation_command(self):
        args = build_parser().parse_args(["trust-next-task"])

        self.assertEqual(args.command, "trust-next-task")
        self.assertFalse(hasattr(args, "yes"))
        speech = Mock()
        with patch("butler.cli.sys.stdin.isatty", return_value=False):
            with redirect_stdout(io.StringIO()):
                result = _trusted_task_control(
                    SimpleNamespace(root=Path("project")), speech
                )
        self.assertEqual(result, 1)
        self.assertIn("Перенаправленный ввод отклонён", speech.say_and_wait.call_args.args[0])

    @patch("butler.cli.TrustedTaskStore")
    @patch("butler.cli.SingleInstance")
    def test_activation_is_rejected_while_an_agent_task_is_running(
        self, lock_class, store_class
    ):
        lock_class.return_value.__enter__.return_value = False
        speech = Mock()

        with patch("butler.cli.sys.stdin.isatty", return_value=True):
            with redirect_stdout(io.StringIO()):
                result = _trusted_task_control(
                    SimpleNamespace(root=Path("project")), speech
                )

        self.assertEqual(result, 1)
        lock_class.assert_called_once_with(Path("project"), "agent-task")
        store_class.assert_not_called()
        self.assertIn("выполняет другой запрос", speech.say_and_wait.call_args.args[0])

    @patch("builtins.input", return_value="1")
    @patch("butler.cli.TrustedTaskStore")
    def test_local_activation_speaks_warning_before_arming(
        self, store_class, _input
    ):
        store = store_class.return_value
        store.status.return_value = None
        speech = Mock()
        sequence = Mock()
        sequence.attach_mock(speech.say_and_wait, "speak")
        sequence.attach_mock(store.arm, "arm")

        with patch("butler.cli.sys.stdin.isatty", return_value=True):
            with redirect_stdout(io.StringIO()):
                result = _trusted_task_control(
                    SimpleNamespace(root=Path("project")), speech
                )

        self.assertEqual(result, 0)
        self.assertIn(TRUSTED_TASK_WARNING, speech.say_and_wait.call_args_list[0].args[0])
        self.assertEqual(sequence.mock_calls[0][0], "speak")
        self.assertEqual(sequence.mock_calls[1][0], "arm")

    def test_release_installs_accessible_local_shortcut(self):
        root = Path(__file__).resolve().parents[1]
        shortcuts = (root / "scripts" / "install-shortcuts.ps1").read_text(
            encoding="utf-8-sig"
        )
        entrypoint = (root / "TRUST-NEXT-TASK.cmd").read_text(encoding="utf-8-sig")
        manifest = json.loads(
            (root / "config" / "release-manifest.json").read_text(encoding="utf-8")
        )

        self.assertIn("Ксения — ДОВЕРЕННАЯ ЗАДАЧА", shortcuts)
        self.assertIn("CTRL+ALT+D", shortcuts)
        self.assertIn("TRUST-NEXT-TASK.cmd", shortcuts)
        self.assertIn("trust-next-task", entrypoint)
        self.assertIn("TRUST-NEXT-TASK.cmd", manifest["package"]["included_files"])


if __name__ == "__main__":
    unittest.main()
