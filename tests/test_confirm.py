"""Подтверждения: вопрос звучит дословно из ядра, события для планшета, устаревание, кривые результаты."""
import asyncio
import time

import pytest

import core
from tools import confirm
from test_core_respond import FakeSpeaker, call, script_steps


@pytest.fixture(autouse=True)
def clean():
    confirm.cancel()
    confirm._listeners.clear()
    yield
    confirm.cancel()
    confirm._listeners.clear()


def test_events_for_screen_buttons():
    events = []
    confirm.on_change(events.append)
    confirm.prepare("сообщение для Димы", lambda: None, question="Пишу Диме: привет. Отправить?")
    confirm.take()
    confirm.prepare("нажать «Удалить»", lambda: None)
    confirm.cancel()
    assert [e["type"] for e in events] == ["confirm", "confirm_clear", "confirm", "confirm_clear"]
    assert events[0]["question"] == "Пишу Диме: привет. Отправить?"
    assert events[2]["question"] == "Нажать «Удалить»?"
    assert [e.get("reason") for e in events if e["type"] == "confirm_clear"] == ["taken", "cancelled"]


def test_expiry_is_announced():
    events = []
    confirm.on_change(events.append)
    confirm.prepare("x", lambda: None, ttl=-1)
    assert confirm.current() == {"expired": True, "label": "x"}
    assert events[-1]["reason"] == "expired"
    assert confirm.take() is None


def test_broken_listener_does_not_break_prepare():
    confirm.on_change(lambda e: 1 / 0)
    assert confirm.prepare("x", lambda: None)


def test_ask_result():
    r = confirm.ask("сообщение", lambda: None, question="Отправить?", to="Дима")
    assert r["prepared"] and r["speak_verbatim"] == "Отправить?" and r["to"] == "Дима" and "не повторяй" in r["note"]


@pytest.fixture
def ks(monkeypatch, tmp_path):
    FakeSpeaker.instances = []
    monkeypatch.setattr(core, "Speaker", FakeSpeaker)
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "history.json"))
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start, k.last_tag, k.speaker, k.session = [], 0, None, None, None
    k.lock, k.last_turn_t = asyncio.Lock(), time.time()
    return k


def test_core_speaks_the_exact_question(ks, monkeypatch):
    async def tool(name, args, session):
        return confirm.ask("сообщение для Димы", lambda: None, question="Пишу Дима Петров: привет. Отправить?")

    monkeypatch.setattr(core, "run_tool", tool)
    script_steps(ks, [("Готовлю.", [call("c", "vk_send")]), ("Жду.", [])])
    asyncio.run(ks.respond("напиши Диме привет", {"_t0": 0}))
    assert ("Пишу Дима Петров: привет. Отправить?", True) in FakeSpeaker.instances[0].spoken


def test_action_returning_garbage_does_not_crash_turn(ks):
    async def weird():
        return None

    confirm.prepare("что-то", weird)
    script_steps(ks, [("Не вышло.", [])])
    asyncio.run(ks.respond("да", {"_t0": 0}))
    assert "НЕ удалось" in ks.history[0]["content"]


def test_turn_context_for_tools(ks):
    seen = []

    async def step(budget, queue, speaker, timings, first_step):
        seen.append(dict(confirm.CONTEXT))
        return "Ок.", [], False

    ks._step = step
    asyncio.run(ks.respond("Да, запомни", {"_t0": 0}))
    asyncio.run(ks.respond("(служебно: …)", {"_t0": 0}, internal=True))
    assert {k: seen[0][k] for k in ("user_text", "internal", "affirmative")} == \
        {"user_text": "Да, запомни", "internal": False, "affirmative": True}
    assert seen[1]["internal"] is True and seen[1]["user_text"] == "" and seen[1]["affirmative"] is False
