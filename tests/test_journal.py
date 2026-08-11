import tempfile
import unittest
from pathlib import Path

from butler.journal import ChangeJournal


class ChangeJournalTests(unittest.TestCase):
    def test_undo_refuses_to_overwrite_a_later_manual_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            runtime = Path(directory) / "runtime"
            root.mkdir()
            target = root / "note.txt"
            target.write_text("до", encoding="utf-8")
            journal = ChangeJournal(root, runtime)
            change = journal.prepare(target)
            target.write_text("Ксения", encoding="utf-8")
            journal.commit(change)
            target.write_text("ручная правка", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "изменён после"):
                journal.undo_last()
            self.assertEqual(target.read_text(encoding="utf-8"), "ручная правка")

    def test_restores_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("до", encoding="utf-8")
            journal = ChangeJournal(root, root / "runtime")
            change = journal.prepare(target)
            target.write_text("после", encoding="utf-8")
            journal.commit(change)
            journal.undo_last()
            self.assertEqual(target.read_text(encoding="utf-8"), "до")

    def test_removes_file_created_by_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "new.txt"
            journal = ChangeJournal(root, root / "runtime")
            change = journal.prepare(target)
            target.write_text("новый", encoding="utf-8")
            journal.commit(change)
            journal.undo_last()
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
