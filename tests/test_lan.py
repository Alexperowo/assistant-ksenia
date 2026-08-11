import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from butler.lan import _address_priority, LanApplication, LanTaskStore, persistent_pin


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

    def test_pin_is_stable_between_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(runtime_dir=Path(directory))
            first = persistent_pin(settings)
            self.assertEqual(first, persistent_pin(settings))
            self.assertEqual(len(first), 6)

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
