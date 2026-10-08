"""Напоминания и погода: «в 7:00» после 7:00, прошедшие даты, пустая отмена, битый файл, осадки по UTC."""
import asyncio
import datetime as dt
import json
import time

import pytest

import core
from tools import daily
from fakes import FakeResponse


class FixedDatetime(dt.datetime):
    NOW = dt.datetime(2026, 10, 8, 7, 30)

    @classmethod
    def now(cls, tz=None):
        return cls.NOW if tz is None else cls.NOW.replace(tzinfo=dt.timezone(dt.timedelta(hours=3))).astimezone(tz)


@pytest.fixture
def rem(monkeypatch, tmp_path):
    f = tmp_path / "reminders.json"
    monkeypatch.setattr(daily, "FILE", str(f))
    return f


def call(name, **args):
    return asyncio.run(daily.call(name, args, None))


def test_seven_oclock_after_seven_is_tomorrow(monkeypatch):
    monkeypatch.setattr(daily.dt, "datetime", FixedDatetime)
    t = daily._parse_at("7:00")
    assert t == dt.datetime(2026, 10, 9, 7, 0)
    assert daily._say_when(t) == "завтра в 07:00"
    assert daily._parse_at("7:45") == dt.datetime(2026, 10, 8, 7, 45)
    assert daily._parse_at("07.45") == dt.datetime(2026, 10, 8, 7, 45)


def test_full_date_in_the_past_is_refused(rem, monkeypatch):
    monkeypatch.setattr(daily.dt, "datetime", FixedDatetime)
    r = call("remind_set", text="позвонить", at="2026-10-01 10:00")
    assert r["ok"] is False and "прошло" in r["error"] and not rem.exists()


@pytest.mark.parametrize("minutes", [-5, 0, "много", 10 ** 12])
def test_bad_minutes(rem, minutes):
    assert call("remind_set", text="x", in_minutes=minutes)["ok"] is False


def test_set_list_due_cancel(rem):
    assert call("remind_set", text="таблетки", in_minutes=1)["ok"]
    assert call("remind_set", text="позвонить маме", in_minutes=30)["ok"]
    assert [r["text"] for r in call("remind_list")["reminders"]] == ["таблетки", "позвонить маме"]
    items = json.loads(rem.read_text(encoding="utf-8"))
    items[0]["ts"] = time.time() - 1
    rem.write_text(json.dumps(items), encoding="utf-8")
    assert [r["text"] for r in daily.due()] == ["таблетки"] and daily.due() == []
    assert call("remind_cancel", query="маме")["cancelled"] == 1


def test_empty_cancel_does_not_wipe_everything(rem):
    call("remind_set", text="таблетки", in_minutes=10)
    assert call("remind_cancel", query=" ")["ok"] is False
    assert len(call("remind_list")["reminders"]) == 1
    assert call("remind_cancel", query="все")["cancelled"] == 1


@pytest.mark.parametrize("content", ['{"a": 1}', '[{"text": "x"}, "мусор", {"ts": "завтра", "text": "y"}]'])
def test_bad_entries_do_not_break_due(rem, content):
    rem.write_text(content, encoding="utf-8")
    assert daily.due() == []


def test_corrupt_file_is_kept_aside(rem, tmp_path):
    rem.write_text("{oops", encoding="utf-8")
    assert daily.due() == []
    assert any(p.name.startswith("reminders.json.bad-") for p in tmp_path.iterdir())


def test_late_reminder_says_so():
    p = core.reminder_prompt({"text": "таблетки", "ts": time.time() - 3600})
    assert "запоздало" in p
    assert "запоздало" not in core.reminder_prompt({"text": "таблетки", "ts": time.time()})


class Session:
    def __init__(self, geo, forecast):
        self.answers = [FakeResponse(200, body=json.dumps(geo)), FakeResponse(200, body=json.dumps(forecast))]

    def get(self, url, **kw):
        return self.answers.pop(0)


def step(when_utc, temp, rain1=None, rain6=None, sym="rain"):
    data = {"instant": {"details": {"air_temperature": temp, "wind_speed": 3, "relative_humidity": 80}}}
    if rain1 is not None:
        data["next_1_hours"] = {"summary": {"symbol_code": sym}, "details": {"precipitation_amount": rain1}}
    if rain6 is not None:
        data["next_6_hours"] = {"summary": {"symbol_code": sym}, "details": {"precipitation_amount": rain6}}
    return {"time": when_utc, "data": data}


def moscow_forecast():
    """Как у met.no: сначала часовые шаги (с next_1_hours и next_6_hours), потом шаги по 6 часов в 0/6/12/18 UTC."""
    ts = []
    t = dt.datetime(2026, 10, 8, 12)
    while t < dt.datetime(2026, 10, 10, 21):  # часовые — до вечера 10.10 по UTC
        ts.append(step(t.strftime("%Y-%m-%dT%H:%M:%SZ"), 5 + t.hour % 7, rain1=0.5, rain6=3.0))
        t += dt.timedelta(hours=1)
    t = dt.datetime(2026, 10, 11, 0)
    while t < dt.datetime(2026, 10, 12, 6):
        ts.append(step(t.strftime("%Y-%m-%dT%H:%M:%SZ"), 2, rain6=2.0))
        t += dt.timedelta(hours=6)
    return {"properties": {"timeseries": ts}}


def weather_in_moscow(monkeypatch, day, now):
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        monkeypatch.setattr(daily.dt, "datetime", type("D", (FixedDatetime,), {"NOW": now}))
        geo = {"results": [{"name": "Москва", "admin1": "Москва", "latitude": 55.75, "longitude": 37.62}]}
        return asyncio.run(daily._weather("Москва", day, Session(geo, moscow_forecast())))
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_rain_tomorrow_hourly_steps(monkeypatch):
    out = weather_in_moscow(monkeypatch, "tomorrow", dt.datetime(2026, 10, 8, 15, 0))
    assert out["forecast"]["date"] == "2026-10-09"
    assert out["forecast"]["precipitation_mm"] == 12.0  # 24 часа × 0,5 мм


def test_rain_on_six_hour_steps_is_not_lost(monkeypatch):
    # 11.10 по Москве — только шестичасовые шаги (03, 09, 15, 21 местного): раньше осадки были 0
    out = weather_in_moscow(monkeypatch, "after_tomorrow", dt.datetime(2026, 10, 9, 15, 0))
    assert out["forecast"]["date"] == "2026-10-11"
    assert out["forecast"]["precipitation_mm"] == 8.0
