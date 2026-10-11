"""Мои настройки голосом — тот же список, что в центре управления."""
import asyncio

import pytest

import control
from tools import prefs


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    c = {}
    monkeypatch.setattr(prefs, "CONFIG", c)
    return c


def call(**args):
    return asyncio.run(prefs.call("ksenia_setting", args, None))


def test_every_center_setting_is_reachable_by_voice():
    keys = prefs.SCHEMAS[0]["function"]["parameters"]["properties"]["key"]["enum"]
    assert keys == [s["key"] for s in control.SETTINGS]


@pytest.mark.parametrize("key,said,want", [
    ("live_doze", "нет", False),
    ("live_doze", "да", True),
    ("watch_poll_s", "каждую минуту", 60),
    ("watch_poll_s", "полторы", 90),
    ("night_gain", "тихо", 0.5),
    ("night_from", "в 22:00", 22),
    ("filler_s", "не говорить", 0),
    ("voice_output", "в колонки монитора", "monitor"),
])
def test_words_to_value(cfg, key, said, want):
    r = call(action="set", key=key, value=said)
    assert r["ok"] and cfg[key] == want
    assert control.load_user_settings()[key] == want  # сохранилось как из центра — переживёт перезапуск


def test_unknown_value_lists_options(cfg):
    r = call(action="set", key="night_gain", value="громко-громко")
    assert not r["ok"] and "Тихо" in r["options"] and "night_gain" not in cfg


def test_ambiguous_partial_is_refused(cfg):
    r = call(action="set", key="night_from", value="в")
    assert not r["ok"]


def test_get_tells_current_in_words(cfg):
    cfg["watch_poll_s"] = 180
    r = call(action="get", key="watch_poll_s")
    assert r["now"] == "Каждые 3 минуты"


def test_blocked_after_untrusted_text():
    import core
    assert "ksenia_setting" in core.TAINT_BLOCKED
