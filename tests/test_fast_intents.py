import unittest
from datetime import datetime

from butler.fast_intents import fast_intent_reply


class FastIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 10, 21, 7)

    def test_date_is_answered_locally(self):
        answer = fast_intent_reply("Какое сегодня число?", now=self.now)
        self.assertEqual(answer, "Сегодня понедельник, 10 августа 2026 года.")

    def test_time_is_answered_locally(self):
        self.assertEqual(
            fast_intent_reply("Который сейчас час?", now=self.now),
            "Сейчас 21:07.",
        )

    def test_mixed_request_is_not_partially_intercepted(self):
        self.assertIsNone(
            fast_intent_reply(
                "Скажи, как тебя зовут, и назови текущую дату", now=self.now
            )
        )
        self.assertIsNone(
            fast_intent_reply(
                "Найди новости, опубликованные на сегодняшнюю дату", now=self.now
            )
        )


if __name__ == "__main__":
    unittest.main()
