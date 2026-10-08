"""Вывод на планшет: звук и события через шлюз, перебивание, запасной путь к колонкам ПК, аренда приглушения."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import core
from tools import confirm
from fakes import FakeResponse, FakeSession
from test_core_speaker import FakeProc, players  # noqa: F401  (фикстура pacat)


@pytest.fixture
def client_q(monkeypatch):
    """Подключённый «шлюз»: очередь, в которую ядро складывает события и звук."""
    monkeypatch.setattr(core, "hub", core.ClientHub())
    q = asyncio.Queue()
    core.hub.queues["gw"] = q
    yield q


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_reply_goes_to_tablet(client_q, players):
    made, _ = players
    sp = core.Speaker(FakeSession(FakeResponse(200, chunks=[b"\x01\x02", b"\x03\x04"])), output="client")

    async def go():
        await sp.speak("[warm] Привет, я на планшете.", {"_t0": 0})
        await sp.finish()

    asyncio.run(go())
    items = drain(client_q)
    kinds = [k if k == "bytes" else p["type"] for k, p in items]
    assert kinds == ["say", "audio_start", "bytes", "bytes", "audio_end"]
    assert items[0][1]["text"] == "Привет, я на планшете."  # без пометки эмоции
    assert b"".join(p for k, p in items if k == "bytes") == b"\x01\x02\x03\x04"
    assert made == []  # pacat не запускался


def test_interrupt_silences_tablet(client_q, players):
    sp = core.Speaker(FakeSession(FakeResponse(200, chunks=[b"\x00\x00"])), output="client")

    async def go():
        await sp.speak("Раз.", {"_t0": 0})
        await sp.cancel()  # новая реплика или «стоп»

    asyncio.run(go())
    types = [p["type"] for k, p in drain(client_q) if k == "json"]
    assert types[-1] == "audio_stop"


def test_no_gateway_falls_back_to_pc(players, monkeypatch):
    made, _ = players
    monkeypatch.setattr(core, "hub", core.ClientHub())  # шлюз не подключён
    sp = core.Speaker(FakeSession(FakeResponse(200, chunks=[b"\x05\x06"])), output="client")

    async def go():
        await sp.speak("Слышно у компьютера.", {"_t0": 0})
        await sp.finish()

    asyncio.run(go())
    assert len(made) == 1 and bytes(made[0].stdin.data) == b"\x05\x06"


def test_local_output_unchanged(client_q, players):
    made, _ = players
    sp = core.Speaker(FakeSession(FakeResponse(200, chunks=[b"\x07\x08"])))

    async def go():
        await sp.speak("Как раньше.", {"_t0": 0})
        await sp.finish()

    asyncio.run(go())
    assert bytes(made[0].stdin.data) == b"\x07\x08"
    assert [p["type"] for k, p in drain(client_q)] == ["say", "state"]  # текст и «говорю у ПК» — на экран


def test_slow_tablet_applies_backpressure(client_q, monkeypatch):
    monkeypatch.setattr(core.ClientHub, "MAX_QUEUE", 3)
    p = core.ClientPlayer()

    async def go():
        for _ in range(5):
            p.stdin.write(b"\x00\x00")
        waiter = asyncio.create_task(p.stdin.drain())
        await asyncio.sleep(0.05)
        blocked = not waiter.done()
        drain(client_q)  # планшет догнал
        await asyncio.wait_for(waiter, 1)
        return blocked

    assert asyncio.run(go())


def test_confirm_question_reaches_tablet(client_q):
    confirm._listeners[:] = [core.hub.emit]
    confirm.prepare("сообщение для Димы", lambda: None, question="Пишу Диме: привет. Отправить?")
    confirm.cancel()
    events = [p for k, p in drain(client_q)]
    assert events[0]["type"] == "confirm" and events[0]["question"] == "Пишу Диме: привет. Отправить?"
    assert events[1]["type"] == "confirm_clear"


def test_preferred_output(client_q, monkeypatch):
    import time
    monkeypatch.setattr(core.ks, "last_client_t", time.time())
    assert core.preferred_output() == "client"
    monkeypatch.setattr(core.ks, "last_client_t", time.time() - 3600)
    assert core.preferred_output() == "local"


def test_say_from_tablet_stops_pc_conversation(monkeypatch):
    seen = {}

    async def fake_turn(text, timings, internal=False, output="local"):
        seen.update(text=text, output=output)
        return "ок"

    async def fake_stop():
        seen["stopped"] = True

    async def no_duck(on):
        pass

    monkeypatch.setattr(core, "turn", fake_turn)
    monkeypatch.setattr(core, "stop_conversation", fake_stop)
    monkeypatch.setattr(core.music, "duck", no_duck)
    monkeypatch.setattr(core, "talk_lock", asyncio.Lock())

    class Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    r = asyncio.run(core.handle_say(Req({"text": "да", "output": "client"})))
    assert r.status == 200 and seen == {"text": "да", "output": "client", "stopped": True}
    assert asyncio.run(core.handle_say(Req({"text": "x", "output": "radio"}))).status == 400


def test_duck_lease(monkeypatch):
    calls = []

    async def duck(on):
        calls.append(on)

    monkeypatch.setattr(core.music, "duck", duck)
    monkeypatch.setitem(core.CONFIG, "client_duck_s", 0.05)
    monkeypatch.setattr(core, "client_duck", {"on": False, "release": None})

    class Req:
        def __init__(self, on):
            self.on = on

        async def json(self):
            return {"on": self.on}

    async def go():
        await core.handle_duck(Req(True))
        await core.handle_duck(Req(True))   # повтор — одна аренда, не вложенность
        await core.handle_duck(Req(False))
        await core.handle_duck(Req(True))
        await asyncio.sleep(0.1)            # планшет пропал посреди записи — аренда истекла сама

    asyncio.run(go())
    assert calls == [True, False, True, False]


def test_client_websocket(monkeypatch):
    monkeypatch.setattr(core, "hub", core.ClientHub())
    confirm.cancel()
    confirm.prepare("x", lambda: None, question="Нажать «Удалить»?")

    async def go():
        app = web.Application(middlewares=[core.local_only])
        app.add_routes([web.get("/client", core.handle_client)])
        async with TestClient(TestServer(app, host="127.0.0.1")) as c:
            ws = await c.ws_connect("/client", headers={"Host": "127.0.0.1:18130"})
            hello = await ws.receive_json()
            await asyncio.sleep(0.05)
            core.hub.emit({"type": "state", "state": "thinking"})
            core.hub.audio(b"\x01\x02")
            ev = await ws.receive_json()
            audio = await ws.receive_bytes()
            await ws.close()
            await asyncio.sleep(0.05)
            return hello, ev, audio, core.hub.connected()

    hello, ev, audio, still = asyncio.run(go())
    confirm.cancel()
    assert hello["type"] == "hello" and hello["confirm"]["question"] == "Нажать «Удалить»?"
    assert ev == {"type": "state", "state": "thinking"} and audio == b"\x01\x02"
    assert still is False  # шлюз отключился — ядро снова говорит у ПК
