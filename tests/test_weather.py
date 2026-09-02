import unittest
from unittest.mock import patch

from butler.config import load_settings
from butler.weather import (
    WeatherClient,
    WeatherObservation,
    _location_candidates,
    extract_weather_location,
)


class WeatherTests(unittest.TestCase):
    def test_location_is_extracted_without_the_request_text(self):
        self.assertEqual(
            extract_weather_location("Какая сегодня погода в Гусь-Хрустальном?"),
            "Гусь-Хрустальном",
        )
        self.assertEqual(extract_weather_location("Какая сейчас погода?"), "")

    def test_locative_adjective_gets_validatable_nominative_candidate(self):
        self.assertIn(
            "Гусь-Хрустальный",
            _location_candidates("Гусь-Хрустальном"),
        )

    @patch.object(WeatherClient, "_json")
    @patch("butler.weather.public_http_url", return_value=True)
    def test_provider_result_is_formatted_without_llm(self, _public_url, request_json):
        request_json.side_effect = [
            {
                "results": [
                    {
                        "name": "Гусь-Хрустальный",
                        "country": "Россия",
                        "latitude": 55.6,
                        "longitude": 40.7,
                    }
                ]
            },
            {
                "current": {
                    "temperature_2m": 12.2,
                    "apparent_temperature": 10.6,
                    "relative_humidity_2m": 73,
                    "precipitation": 0.0,
                    "weather_code": 2,
                    "wind_speed_10m": 8.4,
                    "time": "2026-09-01T12:00",
                }
            },
        ]

        observation = WeatherClient(load_settings()).current("Гусь-Хрустальный")

        self.assertIsNotNone(observation)
        summary = observation.spoken_summary()
        self.assertIn("плюс 12 градусов", summary)
        self.assertIn("Переменная облачность", summary)
        self.assertIn("Осадков сейчас нет", summary)

    def test_unknown_weather_code_remains_honest(self):
        observation = WeatherObservation(
            "Тест",
            "",
            0,
            0,
            50,
            0,
            0,
            999,
            "",
        )
        self.assertIn("без описания", observation.spoken_summary())


if __name__ == "__main__":
    unittest.main()
