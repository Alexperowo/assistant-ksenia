import tempfile
import unittest
from pathlib import Path

from butler.memory import ConversationMemory


class ConversationMemoryTests(unittest.TestCase):
    def test_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = ConversationMemory(Path(directory), max_messages=8)
            messages = [
                {"role": "user", "content": "Запомни задачу"},
                {"role": "assistant", "content": "Запомнила"},
            ]
            memory.save(messages)
            self.assertEqual(memory.load(), messages)
            memory.clear()
            self.assertEqual(memory.load(), [])

    def test_invalid_file_is_recovered_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = ConversationMemory(Path(directory))
            memory.path.parent.mkdir(parents=True)
            memory.path.write_text("broken", encoding="utf-8")
            self.assertEqual(memory.load(), [])


if __name__ == "__main__":
    unittest.main()
