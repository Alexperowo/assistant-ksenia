"""Живой диалог: сбои слуха объясняются голосом, перебивание не плодит разговоры."""
import asyncio
import json

import aiohttp
import pytest

import core
from fakes import FakeResponse, FakeSession


def heard(text="", **timings):
    return FakeResponse(200, body=json.dumps({"text": text, "timings": timings}, ensure_ascii=False))


@pytest.fixture
def env(monkeypatch):
    rec = {"turns": [], "notices": []}

    async def fake_turn(text, timings):
        rec["turns"].append(text)
        return "ок"

    async def fake_notice(text):
        rec["notices"].append(text)

    async def no_duck(on):
        pass

    monkeypatch.setattr(core, "turn", fake_turn)
    monkeypatch.setattr(core, "say_notice", fake_notice)
    monkeypatch.setattr(core.music, "duck", no_duck)
    monkeypatch.setattr(core.ks, "session", None)
    monkeypatch.setitem(core.CONFIG, "listen_busy_wait_s", 5)
    return rec


def run_conv(*responses):
    core.ks.session = FakeSession(*responses)
    asyncio.run(core.Conversation().run())
    return core.ks.session


def test_turns_until_silence(env):
    run_conv(heard("Привет"), heard("Как дела?"), heard(""))
    assert env["turns"] == ["Привет", "Как дела?"] and env["notices"] == []


def test_goodbye_ends_after_answer(env):
    run_conv(heard("Ну всё, пока"))
    assert env["turns"] == ["Ну всё, пока"]


def test_no_microphone_is_said_aloud(env):
    run_conv(FakeResponse(503, body='{"error": "no_microphone"}'))
    assert env["turns"] == [] and "микрофон" in env["notices"][0]


def test_voice_in_down_is_said_aloud(env):
    run_conv(aiohttp.ClientConnectionError("refused"))
    assert "слух не отвечает" in env["notices"][0]


def test_voice_in_crash_with_plain_text_body(env):
    run_conv(FakeResponse(500, body="500 Internal Server Error"))
    assert "слух не отвечает" in env["notices"][0]


def test_busy_listener_is_waited_not_treated_as_silence(env):
    session = run_conv(FakeResponse(409, body='{"error": "busy"}'), heard("Включи радио"), heard(""))
    assert env["turns"] == ["Включи радио"] and len(session.requests) == 3


def test_mic_lost_is_said_aloud(env):
    run_conv(heard("", reason="mic_lost"))
    assert "Микрофон пропал" in env["notices"][0]


def test_crash_in_turn_is_said_aloud(env, monkeypatch):
    async def broken(text, timings):
        raise RuntimeError("bug")

    monkeypatch.setattr(core, "turn", broken)
    run_conv(heard("Привет"))
    assert env["notices"] == [core.CONV_CRASH]


def test_double_press_keeps_one_conversation(monkeypatch):
    started = []

    async def fake_run(self):
        started.append(asyncio.current_task())
        await asyncio.sleep(3600)

    monkeypatch.setattr(core.Conversation, "run", fake_run)
    monkeypatch.setattr(core, "conv", core.Conversation())
    monkeypatch.setattr(core, "talk_lock", asyncio.Lock())

    async def go():
        await asyncio.gather(core.handle_talk(None), core.handle_talk(None))
        await asyncio.sleep(0)
        alive = [t for t in started if not t.done()]
        await core.handle_stop(None)
        return alive, started

    alive, started_tasks = asyncio.run(go())
    assert len(alive) == 1
    assert all(t.done() for t in started_tasks)


def test_say_requires_text():
    class Req:
        async def json(self):
            return {"txt": "опечатка"}

    resp = asyncio.run(core.handle_say(Req()))
    assert resp.status == 400


def test_run_tool_errors_are_readable(monkeypatch):
    async def slow(name, args, session):
        await asyncio.sleep(10)

    monkeypatch.setattr(core, "TOOL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(core.music, "call", slow)
    r = asyncio.run(core.run_tool("music_status", "{}", None))
    assert r["ok"] is False and "не ответил" in r["error"]
    assert "не JSON" in asyncio.run(core.run_tool("music_status", "{oops", None))["error"]
    assert "объектом" in asyncio.run(core.run_tool("music_status", "[1]", None))["error"]
    assert "нет такого" in asyncio.run(core.run_tool("rm_rf", "{}", None))["error"]
