import unittest

from butler.speech_text import integer_to_russian_words, normalize_for_speech


class SpeechTextTests(unittest.TestCase):
    def test_integer_words_cover_context_sizes_and_years(self):
        self.assertEqual(integer_to_russian_words(0), "ноль")
        self.assertEqual(integer_to_russian_words(65536), "шестьдесят пять тысяч пятьсот тридцать шесть")
        self.assertEqual(integer_to_russian_words(-12), "минус двенадцать")

    def test_written_date_is_grammatical_and_has_no_digits(self):
        spoken = normalize_for_speech("Сегодня 10 августа 2026 года.")
        self.assertEqual(spoken, "Сегодня десятое августа две тысячи двадцать шестого года.")

    def test_numeric_date_time_and_generic_numbers_are_audible(self):
        spoken = normalize_for_speech("Дата 10.08.2026, время 18:12, порт 18080, версия 4.0.")
        self.assertNotRegex(spoken, r"\d")
        self.assertIn("десятое августа две тысячи двадцать шестого года", spoken)
        self.assertIn("восемнадцать часов двенадцать минут", spoken)
        self.assertIn("восемнадцать тысяч восемьдесят", spoken)
        self.assertIn("четыре точка ноль", spoken)


if __name__ == "__main__":
    unittest.main()
