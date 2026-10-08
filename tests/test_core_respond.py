"""Ход разговора целиком: история только дописывается, вызовы инструментов всегда получают ответ,
озвучка не переживает ход."""
import asyncio
import json

import pytest

import core


class FakeSpeaker:
    instances = []

    def __init__(self, session, output="local"):
        self.output = output
        self.cancelled = False
        self.spoken = []
        self.finished = False
        FakeSpeaker.instances.append(self)

    async def speak(self, text, timings, verbatim=False):
        if not self.cancelled:
            self.spoken.append((text, verbatim))

    async def finish(self):
        self.finished = True

    async def cancel(self):
        self.cancelled = True


@pytest.fixture
def ks(monkeypatch, tmp_path):
    FakeSpeaker.instances = []
    monkeypatch.setattr(core, "Speaker", FakeSpeaker)
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "history.json"))
    k = core.Ksenia.__new__(core.Ksenia)
    k.history = [{"role": "user", "content": "раньше"}, {"role": "assistant", "content": "было"}]
    k.window_start, k.last_tag, k.speaker, k.session = 0, None, None, None
    k.lock = asyncio.Lock()
    return k


def call(cid, name, args="{}"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def script_steps(ks, steps):
    """Подменить _step: каждый шаг — (текст, вызовы); текст кладётся в очередь озвучки."""
    it = iter(steps)

    async def fake_step(budget, queue, speaker, timings, first_step):
        content, calls = next(it)
        if content:
            await queue.put((content, False))
        return content, calls, False

    ks._step = fake_step


def tool_pairs_ok(history):
    """Каждый assistant.tool_calls сразу за собой имеет tool-ответы на все id."""
    for i, m in enumerate(history):
        if m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            answers = [h["tool_call_id"] for h in history[i + 1:i + 1 + len(ids)] if h["role"] == "tool"]
            if answers != ids:
                return False
    return True


def test_tool_flow_appends_only_and_reads_verbatim(ks, monkeypatch):
    before = [dict(m) for m in ks.history]
    script_steps(ks, [("Читаю.", [call("c1", "clipboard_read")]), ("Дальше читать?", [])])

    async def fake_tool(name, args, session):
        return {"ok": True, "speak_verbatim": "[Глава 1] Текст. Ещё текст."}

    monkeypatch.setattr(core, "run_tool", fake_tool)
    reply = asyncio.run(ks.respond("прочитай выделенное", {"_t0": 0}))
    assert ks.history[:len(before)] == before  # начало истории не тронуто (гибридный Nex)
    roles = [m["role"] for m in ks.history[len(before):]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert "(служебно:" in ks.history[len(before)]["content"]
    sp = FakeSpeaker.instances[0]
    assert ("Читаю.", False) in sp.spoken
    assert any(v and "Глава 1" in t for t, v in sp.spoken)  # дословный текст помечен verbatim
    assert sp.finished and reply == "Читаю. Дальше читать?"
    assert json.load(open(core.HISTORY_FILE, encoding="utf-8"))[-1]["content"] == "Дальше читать?"


def test_stop_during_tool_chain_answers_every_call(ks, monkeypatch):
    script_steps(ks, [("Делаю.", [call("a", "music_status"), call("b", "music_play", '{"query":"jazz"}')])])
    ran = []

    async def fake_tool(name, args, session):
        ran.append(name)
        FakeSpeaker.instances[0].cancelled = True  # Александр сказал «стоп» во время первого инструмента
        return {"ok": True}

    monkeypatch.setattr(core, "run_tool", fake_tool)
    asyncio.run(ks.respond("что играет и включи джаз", {"_t0": 0}))
    assert ran == ["music_status"]  # второй вызов не исполнен
    assert tool_pairs_ok(ks.history)
    last = json.loads(ks.history[-1]["content"])
    assert ks.history[-1]["tool_call_id"] == "b" and last["ok"] is False and "отменено" in last["error"]


def test_task_cancelled_inside_tool_keeps_history_consistent(ks, monkeypatch):
    script_steps(ks, [("Смотрю.", [call("s1", "screen_describe")])])
    started = asyncio.Event()

    async def slow_tool(name, args, session):
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(core, "run_tool", slow_tool)

    async def go():
        t = asyncio.create_task(ks.respond("что на экране", {"_t0": 0}))
        await started.wait()
        t.cancel()  # нажатие клавиши посреди ответа
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.sleep(0)
        others = [x for x in asyncio.all_tasks() if x is not asyncio.current_task()]
        return others

    leftover = asyncio.run(go())
    assert leftover == []  # озвучка не осталась висеть
    assert tool_pairs_ok(ks.history)
    assert ks.history[-1]["role"] == "tool" and "отменено" in ks.history[-1]["content"]
    assert FakeSpeaker.instances[0].cancelled


def test_unexpected_error_does_not_leave_worker(ks):
    async def broken_step(*a, **k):
        raise RuntimeError("bug")

    ks._step = broken_step

    async def go():
        with pytest.raises(RuntimeError):
            await ks.respond("привет", {"_t0": 0})
        await asyncio.sleep(0)
        return [x for x in asyncio.all_tasks() if x is not asyncio.current_task()]

    assert asyncio.run(go()) == []
    assert ks.history[-1]["role"] == "user"

