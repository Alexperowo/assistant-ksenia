import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from butler.lan import _address_priority, LanApplication, LanTaskStore, persistent_pin
from butler.tasking import DurableTaskStore, TaskCancelled, TaskState


class LanTaskStoreTests(unittest.TestCase):
    def test_completed_task_history_is_bounded(self):
        store = LanTaskStore()
        first_id = ""
        latest_id = ""
        for index in range(105):
            task = store.create(f"Задача {index}")
            first_id = first_id or task.id
            latest_id = task.id
            store.update(task.id, "Готово", answer="готово")
        self.assertIsNone(store.get(first_id))
        self.assertIsNotNone(store.get(latest_id))

    def test_home_lan_address_is_preferred_to_vpn_range(self):
        addresses = ["172.18.0.1", "192.168.0.14", "10.0.0.5"]
        self.assertEqual(sorted(addresses, key=_address_priority)[0], "192.168.0.14")

    def test_task_tracks_statuses_and_answer(self):
        store = LanTaskStore()
        task = store.create("Проверь систему")
        store.update(task.id, "Думаю")
        store.update(task.id, "Проверяю")
        result = store.update(task.id, "Готово", answer="Система работает.")

        self.assertTrue(result["done"])
        self.assertEqual(result["answer"], "Система работает.")
        self.assertEqual(
            [event["status"] for event in result["events"]],
            ["В очереди", "Думаю", "Проверяю", "Готово"],
        )

    def test_duplicate_status_is_not_added_twice(self):
        store = LanTaskStore()
        task = store.create("Тест")
        store.update(task.id, "Думаю")
        result = store.update(task.id, "Думаю")
        self.assertEqual(
            [event["status"] for event in result["events"]],
            ["В очереди", "Думаю"],
        )

    def test_confirmation_is_visible_but_task_is_not_done(self):
        store = LanTaskStore()
        task = store.create("Запиши файл")
        result = store.request_confirmation(
            task.id, "write_workspace_file", {"path": "note.txt", "content": "secret"}, "Нужно подтверждение"
        )
        self.assertFalse(result["done"])
        self.assertEqual(result["status"], "Ожидаю подтверждение")
        self.assertEqual(result["confirmation"]["arguments"], {})
        self.assertTrue(result["confirmation"]["confirmation_id"])
        self.assertIsInstance(result["confirmation"]["revision"], int)
        self.assertRegex(result["confirmation"]["digest"], r"^[0-9a-f]{64}$")

    def _confirmation_app(self, directory):
        app = LanApplication.__new__(LanApplication)
        app.settings = SimpleNamespace(runtime_dir=Path(directory))
        app.speech = SimpleNamespace(say=lambda _message: None)
        app.task_journal = DurableTaskStore(Path(directory))
        app.store = LanTaskStore(app.task_journal)
        app._confirmation_lock = threading.Lock()
        app._confirmations = {}
        return app

    @staticmethod
    def _wait_for_confirmation(app, task_id):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            task = app.store.get(task_id)
            if task and task.get("confirmation"):
                return task["confirmation"]
            time.sleep(0.01)
        raise AssertionError("Подтверждение не появилось")

    def test_confirmation_decision_is_exact_and_one_shot(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "butler.lan.diagnostic_event"
        ):
            app = self._confirmation_app(directory)
            task = app.store.create("Запиши файл")
            app.store.update(task.id, "Начинаю")
            result = {}
            worker = threading.Thread(
                target=lambda: result.setdefault(
                    "approved",
                    app._confirm(
                        task.id,
                        "write_workspace_file",
                        {"path": "note.txt", "content": "text"},
                        "Нужно подтверждение",
                    ),
                ),
                daemon=True,
            )
            worker.start()
            confirmation = self._wait_for_confirmation(app, task.id)

            self.assertFalse(
                app.decide(
                    task.id,
                    True,
                    "stale-id",
                    confirmation["revision"],
                    confirmation["digest"],
                )
            )
            self.assertFalse(
                app.decide(
                    task.id,
                    True,
                    confirmation["confirmation_id"],
                    confirmation["revision"] + 1,
                    confirmation["digest"],
                )
            )
            self.assertFalse(
                app.decide(
                    task.id,
                    True,
                    confirmation["confirmation_id"],
                    confirmation["revision"],
                    "0" * 64,
                )
            )
            self.assertTrue(
                app.decide(
                    task.id,
                    True,
                    confirmation["confirmation_id"],
                    confirmation["revision"],
                    confirmation["digest"],
                )
            )
            self.assertFalse(
                app.decide(
                    task.id,
                    True,
                    confirmation["confirmation_id"],
                    confirmation["revision"],
                    confirmation["digest"],
                )
            )
            worker.join(timeout=1)

            self.assertFalse(worker.is_alive())
            self.assertTrue(result["approved"])

    def test_cancel_wakes_confirmation_waiter_without_resurrecting_task(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "butler.lan.diagnostic_event"
        ):
            app = self._confirmation_app(directory)
            task = app.store.create("Запиши файл")
            app.store.update(task.id, "Начинаю")
            result = {}

            def wait_for_decision():
                try:
                    app._confirm(
                        task.id,
                        "write_workspace_file",
                        {"path": "note.txt", "content": "text"},
                        "Нужно подтверждение",
                    )
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    result["error"] = exc

            worker = threading.Thread(target=wait_for_decision, daemon=True)
            worker.start()
            self._wait_for_confirmation(app, task.id)

            cancelled = app.control(task.id, "cancel")
            worker.join(timeout=1)

            self.assertFalse(worker.is_alive())
            self.assertIsInstance(result.get("error"), TaskCancelled)
            self.assertEqual(cancelled["status"], "Отменено")
            self.assertTrue(cancelled["done"])
            current = app.task_journal.get(task.id)
            self.assertEqual(current["state"], TaskState.CANCELLED)
            self.assertEqual(current["status"], "Отменено")

    def test_cancelled_queued_task_cannot_be_started(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = DurableTaskStore(Path(directory))
            store = LanTaskStore(journal)
            task = store.create("Тест")
            store.update(task.id, "Отменено")

            with self.assertRaises(TaskCancelled):
                store.update(
                    task.id,
                    "Начинаю",
                    expected_states={TaskState.QUEUED},
                )

            current = journal.get(task.id)
            self.assertEqual(current["state"], TaskState.CANCELLED)
            self.assertEqual(current["status"], "Отменено")

    def test_web_client_returns_confirmation_identity(self):
        page = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("confirmation_id", page)
        self.assertIn("confirmationRevision", page)
        self.assertIn("confirmationDigest", page)

    def test_pin_is_stable_between_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(runtime_dir=Path(directory))
            first = persistent_pin(settings)
            self.assertEqual(first, persistent_pin(settings))
            self.assertEqual(len(first), 6)

    def test_parallel_first_use_returns_one_persisted_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(runtime_dir=Path(directory))
            with ThreadPoolExecutor(max_workers=8) as pool:
                values = list(pool.map(lambda _index: persistent_pin(settings), range(24)))

            self.assertEqual(len(set(values)), 1)
            self.assertEqual(
                (Path(directory) / "lan" / "pin.txt").read_text(encoding="ascii"),
                values[0],
            )

    def test_pin_rate_limit_blocks_after_ten_failures(self):
        app = LanApplication.__new__(LanApplication)
        app.pin = "123456"
        app._auth_lock = threading.Lock()
        app._auth_failures = {}
        for _ in range(10):
            self.assertFalse(app.authorized("000000", "phone"))
        self.assertFalse(app.authorized("123456", "phone"))


if __name__ == "__main__":
    unittest.main()
