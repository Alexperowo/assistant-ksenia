"""Песочница автопроверок: настоящий ход, но разговор, история на диске и фоновые дела не меняются.
И голосовая справка help_guide."""
import asyncio
import json
import os

import pytest

import core
from tools import confirm, guide
from test_core_respond import FakeSpeaker, call, script_steps


@pytest.fixture
def ks(monkeypatch, tmp_path):
    FakeSpeaker.instances = []
    monkeypatch.setattr(core, "Speaker", FakeSpeaker)
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "history.json"))
    k = core.Ksenia.__new__(core.Ksenia)
    k.history = [{"role": "user", "content": "Это ёлка?"}, {"role": "assistant", "content": "Почти!"}]
    k.window_start, k.last_tag, k.speaker, k.session = 0, None, None, None
    k.lock = asyncio.Lock()
    k.last_turn_t = 123.0
    monkeypatch.setattr(core, "ks", k)
    confirm.cancel()
    yield k
    confirm.cancel()


def test_sandbox_leaves_conversation_untouched(ks, monkeypatch):
    ran = []

    async def fake_tool(name, args, session):
        ran.append(name)
        return {"ok": True, "temp": 15}

    monkeypatch.setattr(core, "run_tool", fake_tool)
    before = [dict(m) for m in ks.history]
    script_steps(ks, [("Смотрю.", [call("w1", "weather", "{}"), call("r1", "research_background", '{"question": "новости"}')]),
                      ("Плюс 15.", [])])
    timings = {"_t0": 0}
    reply = asyncio.run(core.turn("Какая погода и новости?", timings, sandbox=True))
    assert "15" in reply
    assert ks.history == before and ks.last_turn_t == 123.0 and ks._sandbox is False
    assert not os.path.exists(core.HISTORY_FILE)  # история на диск не писалась
    assert ran == ["weather"]  # фоновый поиск не запущен: его находка пришла бы в настоящий разговор
    assert [t["name"] for t in timings["tools"]] == ["weather", "research_background"]


def test_sandbox_restores_state_after_error(ks):
    async def boom(*a, **kw):
        raise RuntimeError("мозг упал")

    ks.respond = boom
    before = list(ks.history)
    with pytest.raises(RuntimeError):
        asyncio.run(core.turn("привет", {"_t0": 0}, sandbox=True))
    assert ks.history == before and ks._sandbox is False


def test_help_guide_sections():
    assert set(guide.SCHEMAS[0]["function"]["parameters"]["properties"]["topic"]["enum"]) == set(guide.SECTIONS)
    r = asyncio.run(guide.call("help_guide", {}, None))
    assert r["ok"] and r["topic"] == "overview"
    for topic, text in guide.SECTIONS.items():
        assert "*" not in text and "#" not in text, topic  # уходит в голос — без разметки
    assert asyncio.run(guide.call("help_guide", {"topic": "нет"}, None))["ok"] is False
    assert "help_guide" in core.TOOL_INDEX
    json.dumps(guide.SCHEMAS, ensure_ascii=False)
