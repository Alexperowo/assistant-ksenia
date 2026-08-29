import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from butler.tasking import DurableTaskStore, TaskCancelled, TaskState


class DurableTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = DurableTaskStore(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_task_survives_new_store_instance(self):
        task = self.store.create("Проверь проект", channel="console")
        self.store.transition(task.id, TaskState.RUNNING, "Выполняю")

        with patch("butler.tasking._process_alive", return_value=False):
            recovered = DurableTaskStore(Path(self.temporary.name)).get(task.id)

        self.assertEqual(recovered["state"], TaskState.INTERRUPTED)
        self.assertTrue(recovered["resumable"])

    def test_live_task_is_not_marked_interrupted_by_second_interface(self):
        task = self.store.create("Проверь проект", channel="console")
        self.store.transition(task.id, TaskState.RUNNING, "Выполняю")

        current = DurableTaskStore(Path(self.temporary.name)).get(task.id)

        self.assertEqual(current["state"], TaskState.RUNNING)

    def test_pause_blocks_until_resume(self):
        task = self.store.create("Тест", channel="voice")
        self.store.request_pause(task.id)
        passed = threading.Event()

        worker = threading.Thread(
            target=lambda: (self.store.checkpoint(task.id), passed.set()), daemon=True
        )
        worker.start()
        time.sleep(0.08)
        self.assertFalse(passed.is_set())
        self.store.resume(task.id)
        worker.join(timeout=1)
        self.assertTrue(passed.is_set())

    def test_cancel_is_observed_at_checkpoint(self):
        task = self.store.create("Тест", channel="lan")
        self.store.cancel(task.id)
        with self.assertRaises(TaskCancelled):
            self.store.checkpoint(task.id)

    def test_terminal_state_is_absorbing(self):
        task = self.store.create("Тест", channel="lan")
        cancelled = self.store.cancel(task.id)

        with self.assertRaises(ValueError):
            self.store.transition(task.id, TaskState.RUNNING, "Начинаю снова")

        current = self.store.get(task.id)
        self.assertEqual(current["state"], TaskState.CANCELLED)
        self.assertEqual(current["status"], "Отменено")
        self.assertEqual(current.get("revision"), cancelled.get("revision"))

    def test_transition_rejects_stale_revision_without_mutating_task(self):
        task = self.store.create("Тест", channel="lan")
        initial_revision = task.snapshot().get("revision", 0)
        running = self.store.transition(
            task.id,
            TaskState.RUNNING,
            "Выполняю",
            expected_revision=initial_revision,
        )

        with self.assertRaises(ValueError):
            self.store.transition(
                task.id,
                TaskState.VERIFYING,
                "Проверяю",
                expected_revision=initial_revision,
            )

        current = self.store.get(task.id)
        self.assertEqual(current["state"], TaskState.RUNNING)
        self.assertEqual(current["status"], "Выполняю")
        self.assertEqual(current.get("revision"), running.get("revision"))

    def test_confirmation_payload_can_be_cleared(self):
        task = self.store.create("Отправь", channel="lan")
        self.store.transition(
            task.id,
            TaskState.WAITING_CONFIRMATION,
            "Ожидаю подтверждение",
            confirmation={"tool": "send_message"},
        )
        current = self.store.transition(
            task.id, TaskState.RUNNING, "Продолжаю", confirmation=None
        )
        self.assertIsNone(current["confirmation"])

    def test_live_generated_and_spoken_answers_are_stored_separately(self):
        task = self.store.create("Расскажи", channel="voice")

        current = self.store.transition(
            task.id,
            TaskState.COMPLETED,
            "Готово",
            answer="Первая. Вторая.",
            generated_answer="Первая. Вторая.",
            spoken_answer="Первая.",
            resumable=False,
        )
        restored = DurableTaskStore(Path(self.temporary.name)).get(task.id)

        self.assertEqual(current["generated_answer"], "Первая. Вторая.")
        self.assertEqual(current["spoken_answer"], "Первая.")
        self.assertEqual(restored["generated_answer"], "Первая. Вторая.")
        self.assertEqual(restored["spoken_answer"], "Первая.")


if __name__ == "__main__":
    unittest.main()
