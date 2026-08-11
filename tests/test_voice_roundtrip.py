import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from test_speech_models import evaluate_roundtrip  # noqa: E402


SYNTHESIZED = (
    "Ксения слушай. Александр проверяет длинную русскую речь. "
    "Сегодня десятое августа две тысячи двадцать шестого года. "
    "Первый фрагмент звучит полностью. Второй фрагмент сохраняет середину. "
    "Третий фрагмент подтверждает конец. Контрольное слово горизонт."
)


class VoiceRoundtripEvaluationTests(unittest.TestCase):
    def test_complete_beginning_middle_and_end_pass(self):
        result = evaluate_roundtrip(
            SYNTHESIZED,
            (
                "Ксения слушай. Александр проверяет речь. Сегодня 10 августа. "
                "Первый фрагмент. Второй фрагмент. Третий фрагмент. Горизонт."
            ),
            chunk_count=2,
            audio_duration_seconds=18.0,
            language_probability=0.99,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing_words"], [])
        self.assertEqual(result["landmark_recall"], 1.0)

    def test_only_beginning_and_end_cannot_hide_a_lost_middle(self):
        result = evaluate_roundtrip(
            SYNTHESIZED,
            "Ксения. Александр. Третий фрагмент. Горизонт.",
            chunk_count=2,
            audio_duration_seconds=18.0,
            language_probability=0.99,
        )
        self.assertFalse(result["ok"])
        self.assertIn("первый", result["missing_words"])
        self.assertIn("второй", result["missing_words"])

    def test_digits_or_suspiciously_short_audio_fail(self):
        result = evaluate_roundtrip(
            SYNTHESIZED.replace("десятое", "10"),
            "Ксения Александр августа первый второй третий горизонт",
            chunk_count=2,
            audio_duration_seconds=3.0,
            language_probability=0.99,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["digits_removed_before_tts"])
        self.assertGreater(result["minimum_audio_duration_seconds"], 3.0)


if __name__ == "__main__":
    unittest.main()
