import tempfile
import unittest
from pathlib import Path

from butler.handoff import RoleHandoffStore


class RoleHandoffStoreTests(unittest.TestCase):
    def test_artifacts_are_ordered_and_shared_between_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RoleHandoffStore(Path(directory))

            store.append("a" * 32, "assistant", "request", "Исходная задача")
            store.append(
                "a" * 32,
                "researcher",
                "plan",
                "Проверить два источника",
                metadata={"source_count": 2},
            )
            store.append("a" * 32, "developer", "result", "Тесты прошли")

            items = store.list_task("a" * 32)
            self.assertEqual([item.role for item in items], ["assistant", "researcher", "developer"])
            self.assertEqual(items[1].metadata["source_count"], 2)
            rendered = store.render_task("a" * 32)
            self.assertIn("[researcher: plan]", rendered)
            self.assertIn("Тесты прошли", rendered)

    def test_invalid_task_identifier_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RoleHandoffStore(Path(directory))
            with self.assertRaises(ValueError):
                store.append("../outside", "assistant", "request", "Нет")

    def test_health_checks_database_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RoleHandoffStore(Path(directory))
            store.append("a" * 32, "assistant", "request", "Проверить память")
            health = store.health()
            self.assertEqual(health["items"], 1)
            self.assertEqual(health["integrity"], "ok")


if __name__ == "__main__":
    unittest.main()
