from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from butler.browser import public_http_url
from butler.config import Settings


class WeatherError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise WeatherError("Сервис погоды неожиданно перенаправил запрос.")


@dataclass(frozen=True)
class WeatherObservation:
    place: str
    country: str
    temperature_c: float
    apparent_temperature_c: float
    humidity_percent: int
    precipitation_mm: float
    wind_kmh: float
    weather_code: int
    observed_at: str

    @property
    def condition(self) -> str:
        descriptions = {
            0: "ясно",
            1: "преимущественно ясно",
            2: "переменная облачность",
            3: "пасмурно",
            45: "туман",
            48: "изморозь и туман",
            51: "слабая морось",
            53: "морось",
            55: "сильная морось",
            56: "слабая ледяная морось",
            57: "ледяная морось",
            61: "слабый дождь",
            63: "дождь",
            65: "сильный дождь",
            66: "слабый ледяной дождь",
            67: "ледяной дождь",
            71: "слабый снег",
            73: "снег",
            75: "сильный снег",
            77: "снежная крупа",
            80: "слабый ливень",
            81: "ливень",
            82: "сильный ливень",
            85: "слабый снегопад",
            86: "сильный снегопад",
            95: "гроза",
            96: "гроза с небольшим градом",
            99: "гроза с сильным градом",
        }
        return descriptions.get(self.weather_code, "погодные условия без описания")

    def spoken_summary(self) -> str:
        temperature = round(self.temperature_c)
        apparent = round(self.apparent_temperature_c)
        temperature_text = f"плюс {temperature}" if temperature > 0 else str(temperature)
        apparent_text = f"плюс {apparent}" if apparent > 0 else str(apparent)
        precipitation = (
            f" Осадков сейчас {self.precipitation_mm:g} миллиметра."
            if self.precipitation_mm > 0
            else " Осадков сейчас нет."
        )
        return (
            f"Сейчас в городе {self.place} {temperature_text} градусов, "
            f"ощущается как {apparent_text}. {self.condition.capitalize()}."
            f" Влажность {self.humidity_percent} процентов, ветер "
            f"{round(self.wind_kmh)} километров в час.{precipitation}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "place": self.place,
            "country": self.country,
            "temperature_c": self.temperature_c,
            "apparent_temperature_c": self.apparent_temperature_c,
            "humidity_percent": self.humidity_percent,
            "precipitation_mm": self.precipitation_mm,
            "wind_kmh": self.wind_kmh,
            "weather_code": self.weather_code,
            "observed_at": self.observed_at,
            "summary": self.spoken_summary(),
        }


def extract_weather_location(text: str) -> str:
    """Extract only the place phrase; resolution remains the provider's job."""

    clean = " ".join(str(text).strip().strip(" .?!,;:").split())
    matches = list(re.finditer(r"\b(?:в|во)\s+(.+)$", clean, flags=re.IGNORECASE))
    if not matches:
        return ""
    location = matches[-1].group(1).strip(" .?!,;:")
    location = re.sub(
        r"\s+(?:сегодня|сейчас|в данный момент)$",
        "",
        location,
        flags=re.IGNORECASE,
    ).strip()
    return location[:160]


def _location_candidates(location: str) -> tuple[str, ...]:
    candidates = [" ".join(location.split())]
    words = candidates[0].split()
    if words:
        last = words[-1]
        variants: list[str] = []
        if last.casefold().endswith("ом") and len(last) > 4:
            variants.extend((last[:-2] + "ый", last[:-2] + "ой"))
        if last.casefold().endswith("ем") and len(last) > 4:
            variants.append(last[:-2] + "ий")
        if last.casefold().endswith("е") and len(last) > 3:
            variants.extend((last[:-1], last[:-1] + "а"))
        for variant in variants:
            candidates.append(" ".join([*words[:-1], variant]))
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


class WeatherClient:
    def __init__(self, settings: Settings) -> None:
        config = settings.raw.get("weather", {})
        if not isinstance(config, Mapping) or not bool(config.get("enabled", True)):
            raise WeatherError("Быстрый сервис текущей погоды отключён.")
        self.geocoding_url = str(config.get("geocoding_url", "")).strip()
        self.forecast_url = str(config.get("forecast_url", "")).strip()
        if not public_http_url(self.geocoding_url) or not public_http_url(self.forecast_url):
            raise WeatherError("Адрес сервиса погоды не прошёл сетевую проверку.")
        self.timeout = max(1.0, min(30.0, float(config.get("timeout_seconds", 8))))
        self.max_bytes = max(
            16_384, min(1_048_576, int(config.get("max_response_bytes", 262_144)))
        )
        self.preferred_country_codes = tuple(
            str(code).strip().upper()
            for code in config.get("preferred_country_codes", [])
            if str(code).strip()
        )
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _json(self, base_url: str, parameters: Mapping[str, object]) -> dict[str, Any]:
        url = base_url + "?" + urllib.parse.urlencode(parameters)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "Assistant-Ksenia/0.1"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                content_type = str(response.headers.get("Content-Type", "")).casefold()
                if "json" not in content_type:
                    raise WeatherError("Сервис погоды вернул данные неизвестного формата.")
                body = response.read(self.max_bytes + 1)
        except WeatherError:
            raise
        except (OSError, ValueError) as exc:
            raise WeatherError("Сервис текущей погоды временно недоступен.") from exc
        if len(body) > self.max_bytes:
            raise WeatherError("Ответ сервиса погоды превысил безопасный лимит.")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WeatherError("Сервис погоды вернул повреждённый ответ.") from exc
        if not isinstance(data, dict):
            raise WeatherError("Сервис погоды вернул повреждённый ответ.")
        return data

    def current(self, location: str) -> WeatherObservation | None:
        place: Mapping[str, Any] | None = None
        for candidate in _location_candidates(location):
            geocoded = self._json(
                self.geocoding_url,
                {"name": candidate, "count": 5, "language": "ru", "format": "json"},
            )
            results = geocoded.get("results", [])
            if isinstance(results, list):
                valid = [result for result in results if isinstance(result, Mapping)]
                preferred = [
                    result
                    for result in valid
                    if str(result.get("country_code", "")).upper()
                    in self.preferred_country_codes
                ]
                if preferred or valid:
                    place = (preferred or valid)[0]
                    break
        if place is None:
            return None
        try:
            forecast = self._json(
                self.forecast_url,
                {
                    "latitude": float(place["latitude"]),
                    "longitude": float(place["longitude"]),
                    "current": (
                        "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "precipitation,weather_code,wind_speed_10m"
                    ),
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            current = forecast["current"]
            if not isinstance(current, Mapping):
                raise TypeError("current")
            return WeatherObservation(
                place=str(place.get("name", location)),
                country=str(place.get("country", "")),
                temperature_c=float(current["temperature_2m"]),
                apparent_temperature_c=float(current["apparent_temperature"]),
                humidity_percent=int(current["relative_humidity_2m"]),
                precipitation_mm=float(current["precipitation"]),
                wind_kmh=float(current["wind_speed_10m"]),
                weather_code=int(current["weather_code"]),
                observed_at=str(current.get("time", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError("Сервис погоды вернул неполные текущие данные.") from exc
