import json
import tempfile
import unittest
from pathlib import Path

from butler.procedures import ProcedureError, ProcedureLibrary


class ProcedureLibraryTests(unittest.TestCase):
    def test_lists_and_reads_valid_procedure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "procedures").mkdir()
            (root / "procedures" / "test.json").write_text(
                json.dumps({"title": "Тест", "purpose": "Проверка", "steps": ["Шаг"]}),
                encoding="utf-8",
            )
            library = ProcedureLibrary(root)
            self.assertEqual(library.list()[0]["name"], "test")
            self.assertEqual(library.read("test")["steps"], ["Шаг"])

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProcedureError):
                ProcedureLibrary(Path(directory)).read("../secret")


if __name__ == "__main__":
    unittest.main()
