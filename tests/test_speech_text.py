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


    def test_transliteration_of_models_and_hardware_terms(self):
        spoken = normalize_for_speech("Я — Qwen3.5, модель от Alibaba Cloud.")
        self.assertNotIn("Qwen", spoken)
        self.assertNotIn("Alibaba", spoken)
        self.assertIn("Квен", spoken)
        self.assertIn("три точка пять", spoken)
        self.assertIn("Алибаба", spoken)

        devices = normalize_for_speech("Шлем Quest 3 на Windows 11 через JBL Tour One M3.")
        self.assertNotIn("Quest", devices)
        self.assertNotIn("Windows", devices)
        self.assertNotIn("JBL", devices)
        self.assertIn("Квест три", devices)
        self.assertIn("Виндовс одиннадцать", devices)
        self.assertIn("Джи-Би-Эль", devices)
        self.assertIn("Ван", devices)

    def test_ellipsis_and_dots_do_not_produce_unwanted_stutter(self):
        spoken = normalize_for_speech("Ожидание... Завершено...")
        self.assertNotIn("точки", spoken)
        self.assertNotIn("точка", spoken)
        self.assertIn("Ожидание", spoken)
        self.assertIn("Завершено", spoken)

    def test_russian_stress_accents(self):
        spoken = normalize_for_speech("Голос готов. Всё готово. Я готова. Сервер готов к работе.")
        self.assertIn("гот+ов", spoken)
        self.assertIn("гот+ово", spoken)
        self.assertIn("гот+ова", spoken)

    def test_arbitrary_latin_is_converted_to_cyrillic(self):
        spoken = normalize_for_speech("Фреймворк OpenHands и бенчмарк.")
        self.assertNotRegex(spoken, r"[A-Za-z]")


if __name__ == "__main__":
    unittest.main()
