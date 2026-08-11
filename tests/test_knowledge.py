import tempfile
import unittest
from pathlib import Path

from butler.knowledge import KnowledgeStore


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_remember_search_and_forget(self):
        item = self.store.remember(
            "Александр предпочитает голос Ксения", category="предпочтения"
        )
        found = self.store.search("голос Ксения")
        self.assertEqual([value.id for value in found], [item.id])
        self.assertTrue(self.store.forget(item.id))
        self.assertEqual(self.store.search("Ксения"), [])

    def test_duplicate_fact_is_updated_not_duplicated(self):
        first = self.store.remember("Контекст 64K", category="проект")
        second = self.store.remember("  Контекст   64K  ", category="настройки")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.search("Контекст")), 1)
        self.assertEqual(self.store.search("Контекст")[0].category, "настройки")

    def test_empty_fact_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.remember("   ")

    def test_health_checks_database_integrity(self):
        self.store.remember("Александр предпочитает голос Ксении")
        health = self.store.health()
        self.assertEqual(health["items"], 1)
        self.assertEqual(health["integrity"], "ok")


if __name__ == "__main__":
    unittest.main()
