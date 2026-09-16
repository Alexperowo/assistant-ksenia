import queue
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from butler.agent import AgentReply, AgentToolEvent
from butler.background_research import (
    BackgroundResearchManager,
    BackgroundResearchResult,
    IsolatedResearchSession,
    is_research_cancel_command,
    is_research_pause_command,
    is_research_resume_command,
    is_research_status_command,
)
from butler.chat import ChatError
from butler.config import load_settings
from butler.handoff import RoleHandoffStore
from butler.orchestrator import RoutedAgentSession
from butler.research import ResearchError
from butler.tasking import DurableTaskStore, TaskCancelled, TaskState
from butler.tools import ToolResult


class BackgroundResearchTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        base = load_settings()
        raw = deepcopy(base.raw)
        raw["live"].update(enabled=False, speech_barge_in=False)
        raw["headset_controls"]["enabled"] = False
        raw["developer"]["workspace_dir"] = "workspace"
        self.settings = replace(base, root=root, runtime_dir=root / "runtime", raw=raw)
        self.handoffs = RoleHandoffStore(self.settings.runtime_dir)
        self.task_store = DurableTaskStore(self.settings.runtime_dir)
        self.manager = BackgroundResearchManager(
            self.settings, self.handoffs, self.task_store
        )

    def test_command_parsers(self):
        # Status command
        self.assertTrue(is_research_status_command("Что нашла?"))
        self.assertTrue(is_research_status_command("что нашел"))
        self.assertTrue(is_research_status_command("как там поиск"))
        self.assertTrue(is_research_status_command("статус поиска"))
        self.assertTrue(is_research_status_command("результаты поиска"))
        self.assertFalse(is_research_status_command("найди новости"))
        self.assertFalse(is_research_status_command("какая погода"))

        # Cancel command
        self.assertTrue(is_research_cancel_command("Отмени поиск"))
        self.assertTrue(is_research_cancel_command("останови поиск"))
        self.assertTrue(is_research_cancel_command("хватит искать"))
        self.assertTrue(is_research_cancel_command("прекрати поиск"))
        self.assertFalse(is_research_cancel_command("найди новости"))

        # Pause command
        self.assertTrue(is_research_pause_command("подожди поиск"))
        self.assertTrue(is_research_pause_command("пауза поиска"))
        self.assertTrue(is_research_pause_command("приостанови поиск"))
        self.assertFalse(is_research_pause_command("продолжи поиск"))

        # Resume command
        self.assertTrue(is_research_resume_command("продолжи поиск"))
        self.assertTrue(is_research_resume_command("возобнови поиск"))
        self.assertFalse(is_research_resume_command("подожди поиск"))

    def test_start_and_complete_research(self):
        gate = threading.Event()
        statuses = []

        def fake_run(request, session, **kwargs):
            on_status = kwargs.get("on_status")
            if on_status:
                on_status("Поиск страниц")
            gate.wait(timeout=2.0)
            return AgentReply(
                "Результат поиска о Марсе.",
                (AgentToolEvent("browser_search", {"query": "Марс"}, ToolResult(True, "ok", "ok")),),
            )

        with patch("butler.background_research.ResearchCoordinator.run", side_effect=fake_run):
            task_id = self.manager.start_research("Марс", on_status=statuses.append)
            self.assertTrue(self.manager.is_busy())
            self.assertEqual(self.manager.get_active_task_id(), task_id)
            self.assertEqual(self.manager.get_active_request(), "Марс")
            self.assertIn("Марс", self.manager.get_status())

            # Attempting to start another task while busy must raise RuntimeError
            with self.assertRaises(RuntimeError):
                self.manager.start_research("Венера")

            # Let the worker complete
            gate.set()
            self.manager._worker_thread.join(timeout=2.0)

            self.assertFalse(self.manager.is_busy())
            self.assertIsNone(self.manager.get_active_task_id())

            # Result queue
            result = self.manager.poll_completed_result()
            self.assertIsNotNone(result)
            self.assertEqual(result.task_id, task_id)
            self.assertEqual(result.request, "Марс")
            self.assertEqual(result.answer, "Результат поиска о Марсе.")
            self.assertEqual(len(result.tool_events), 1)
            self.assertFalse(result.cancelled)
            self.assertEqual(result.error, "")

            # Second poll returns None
            self.assertIsNone(self.manager.poll_completed_result())

            # Task store record completed
            record = self.task_store.get(task_id)
            self.assertEqual(record["state"], TaskState.COMPLETED)
            self.assertEqual(record["answer"], "Результат поиска о Марсе.")

    def test_cancel_active_research(self):
        gate = threading.Event()

        def fake_run(request, session, **kwargs):
            control = kwargs.get("control")
            # Wait for cancellation signal
            while not gate.is_set():
                if control:
                    control.checkpoint()
                time.sleep(0.01)
            raise TaskCancelled("Cancelled by user")

        with patch("butler.background_research.ResearchCoordinator.run", side_effect=fake_run):
            task_id = self.manager.start_research("Новости космоса")
            self.assertTrue(self.manager.is_busy())

            # Request cancellation
            cancelled = self.manager.cancel_active()
            self.assertTrue(cancelled)

            # Signal worker loop to check checkpoint
            gate.set()
            self.manager._worker_thread.join(timeout=2.0)

            self.assertFalse(self.manager.is_busy())
            result = self.manager.poll_completed_result()
            self.assertIsNotNone(result)
            self.assertTrue(result.cancelled)

            record = self.task_store.get(task_id)
            self.assertEqual(record["state"], TaskState.CANCELLED)

    def test_pause_and_resume_active_research(self):
        gate = threading.Event()

        def fake_run(request, session, **kwargs):
            gate.wait(timeout=2.0)
            return AgentReply("Ответ", ())

        with patch("butler.background_research.ResearchCoordinator.run", side_effect=fake_run):
            task_id = self.manager.start_research("Тест паузы")
            self.assertTrue(self.manager.is_busy())
            try:
                self.assertTrue(self.manager.pause_active())
                record = self.task_store.get(task_id)
                self.assertEqual(record["state"], TaskState.PAUSED)

                self.assertTrue(self.manager.resume_active())
                record_resumed = self.task_store.get(task_id)
                self.assertEqual(record_resumed["state"], TaskState.RUNNING)
            finally:
                gate.set()
                if self.manager._worker_thread:
                    self.manager._worker_thread.join(timeout=2.0)

    def test_failed_research_handling(self):
        def fake_run(request, session, **kwargs):
            raise RuntimeError("Сеть недоступна")

        with patch("butler.background_research.ResearchCoordinator.run", side_effect=fake_run):
            task_id = self.manager.start_research("Тест ошибки")
            self.manager._worker_thread.join(timeout=2.0)

            self.assertFalse(self.manager.is_busy())
            result = self.manager.poll_completed_result()
            self.assertIsNotNone(result)
            self.assertEqual(result.error, "Сеть недоступна")
            self.assertEqual(result.answer, "")

            record = self.task_store.get(task_id)
            self.assertEqual(record["state"], TaskState.FAILED)
            self.assertIn("Сеть недоступна", record.get("error", ""))

    def test_isolated_session_does_not_leak_to_outer(self):
        session = IsolatedResearchSession(self.settings)
        self.assertIsNotNone(session.tools)
        self.assertIs(session.settings, self.settings)

    @patch("butler.orchestrator.ModelManager.for_role")
    def test_orchestrator_thinking_fallback_when_research_busy(self, for_role):
        mock_mgr = Mock()
        mock_mgr.is_current.return_value = True
        for_role.return_value = mock_mgr

        session = RoutedAgentSession(self.settings)
        session.residency.activate_residents = Mock(return_value={})
        session._assistant_mode_override = "thinking"
        session.session.ask = Mock(
            return_value=AgentReply("Быстрый ответ без размышлений", ())
        )

        # Mock background research as busy
        with patch.object(session.background_research, "is_busy", return_value=True):
            statuses = []
            reply = session.ask("Почему трава зеленая?", on_status=statuses.append)
            self.assertEqual(reply.text, "Быстрый ответ без размышлений")
            self.assertTrue(
                any("Исследователь занят поиском" in s for s in statuses)
            )

    @patch("butler.orchestrator.ModelManager.for_role")
    def test_orchestrator_non_exclusive_task_bypasses_lock(self, for_role):
        mock_mgr = Mock()
        mock_mgr.is_current.return_value = True
        for_role.return_value = mock_mgr

        session = RoutedAgentSession(self.settings)
        session.residency.activate_residents = Mock(return_value={})
        session.session.ask = Mock(
            return_value=AgentReply("Ответ на обычный чат", ())
        )

        with patch.object(session.background_research, "is_busy", return_value=True):
            # A regular conversation query does not need exclusive lock and does not fail if research is busy
            reply = session.ask("Расскажи шутку")
            self.assertEqual(reply.text, "Ответ на обычный чат")

    def test_orchestrator_exclusive_task_rejects_when_research_busy(self):
        session = RoutedAgentSession(self.settings)

        with patch.object(session.background_research, "is_busy", return_value=True):
            # Mutating developer action is exclusive
            with self.assertRaises(ChatError) as ctx:
                session.ask("Создай файл test.txt")
            self.assertIn("Выполняется фоновый поиск", str(ctx.exception))

            # A new web research request is also rejected while one is active
            with self.assertRaises(ChatError) as ctx2:
                session.ask("Найди в интернете рецепт пирога")
            self.assertIn("Выполняется фоновый поиск", str(ctx2.exception))

    def test_orchestrator_research_control_commands(self):
        session = RoutedAgentSession(self.settings)

        # Status command when no research is running
        reply = session.ask("Что нашла?")
        self.assertIn("нет активного поиска", reply.text)

        # Status command when research is running
        with patch.object(session.background_research, "is_busy", return_value=True):
            with patch.object(
                session.background_research,
                "get_status",
                return_value="Поиск по запросу «Марс» выполняется: читаю страницу.",
            ):
                reply = session.ask("Что нашла?")
                self.assertIn("Поиск по запросу «Марс» выполняется", reply.text)

            # Cancel command
            with patch.object(session.background_research, "cancel_active", return_value=True):
                reply = session.ask("Отмени поиск")
                self.assertIn("Поиск отмен", reply.text)


if __name__ == "__main__":
    unittest.main()
