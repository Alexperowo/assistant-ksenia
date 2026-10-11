"""Центр управления: карточка «Компьютер» и честная громкость (анализ Codex, 3.2 и 2.2)."""
import asyncio
import json

import core
from tools import settings


class Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def act(monkeypatch, body, result):
    calls = []

    async def fake(name, args, session):
        calls.append(args)
        return result
    monkeypatch.setattr(settings, "call", fake)
    r = asyncio.run(core.handle_control_act(Req(body)))
    return json.loads(r.body), calls


def test_brightness_up(monkeypatch):
    out, calls = act(monkeypatch, {"action": "computer", "do": "brightness_up"}, {"ok": True, "brightness": 65})
    assert calls == [{"action": "brightness_up", "value": ""}] and out["ok"] and "65" in out["say"]


def test_theme_dark(monkeypatch):
    out, calls = act(monkeypatch, {"action": "computer", "do": "theme_dark"}, {"ok": True, "theme": "BreezeDark"})
    assert calls == [{"action": "theme", "value": "dark"}] and "Тёмная" in out["say"]


def test_unknown_computer_action_is_refused(monkeypatch):
    out, calls = act(monkeypatch, {"action": "computer", "do": "rm_rf"}, {"ok": True})
    assert not out["ok"] and calls == []


def test_volume_failure_is_not_reported_as_success(monkeypatch):
    out, _ = act(monkeypatch, {"action": "volume", "step": "up"}, {"ok": False, "error": "громкость не изменилась"})
    assert not out["ok"] and "Громкость 5" not in out["say"]
