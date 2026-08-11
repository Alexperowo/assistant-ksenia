import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from voice_worker import split_tts_text  # noqa: E402


class VoiceWorkerTests(unittest.TestCase):
    def test_long_text_is_split_without_losing_words(self):
        text = (
            "Первое достаточно длинное предложение для проверки. " * 12
            + "Последняя часть ответа должна обязательно прозвучать."
        )
        chunks = split_tts_text(text, max_chars=140)
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) <= 140 for chunk in chunks))
        self.assertEqual(" ".join(chunks), " ".join(text.split()))
        self.assertTrue(chunks[-1].endswith("прозвучать."))


if __name__ == "__main__":
    unittest.main()
