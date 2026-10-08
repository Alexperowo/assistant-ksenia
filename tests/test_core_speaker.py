"""Озвучка: сбои голоса и плеера не глушат ответ и не оставляют висящих процессов."""
import asyncio

import aiohttp
import pytest

import core
from fakes import FakeResponse, FakeSession


class FakeStdin:
    def __init__(self, fail_on_drain=False):
        self.data = bytearray()
        self.closed = False
        self.fail_on_drain = fail_on_drain

    def write(self, b):
        self.data.extend(b)

    async def drain(self):
        if self.fail_on_drain:
            raise ConnectionResetError("Connection lost")

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, fail_on_drain=False, hang=False):
        self.stdin = FakeStdin(fail_on_drain)
        self.returncode = None
        self.killed = False
        self.hang = hang

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        while self.hang and not self.killed:
            await asyncio.sleep(0.01)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture
def players(monkeypatch):
    """Каждый запуск pacat отдаёт следующий FakeProc из списка (по умолчанию — исправный)."""
    made, plan = [], []

    async def fake_exec(*args, **kw):
        assert args[0] == "pacat"
        p = plan.pop(0) if plan else FakeProc()
        made.append(p)
        return p

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(core, "pick_output_sink", lambda: None)
    monkeypatch.setitem(core.CONFIG, "record_replies", False)
    return made, plan


def speak_all(session, phrases, speaker=None):
    sp = speaker or core.Speaker(session)

    async def go():
        for ph in phrases:
            await sp.speak(ph, {"_t0": 0})
        await sp.finish()
        return sp

    return asyncio.run(go())


def test_player_dies_next_phrase_gets_new_player(players):
    made, plan = players
    plan.append(FakeProc(fail_on_drain=True))  # наушники отключились посреди фразы
    session = FakeSession(FakeResponse(200, chunks=[b"\x01\x02"]), FakeResponse(200, chunks=[b"\x03\x04"]))
    speak_all(session, ["Первая фраза.", "Вторая фраза."])
    assert len(made) == 2 and made[0].killed
    assert bytes(made[1].stdin.data) == b"\x03\x04" and made[1].stdin.closed


def test_broken_stream_is_padded_to_whole_samples(players):
    made, _ = players
    session = FakeSession(FakeResponse(200, chunks=[b"\x01\x02\x03"], error=aiohttp.ClientPayloadError("cut")),
                          FakeResponse(200, chunks=[b"\x10\x20"]))
    speak_all(session, ["Обрыв.", "Дальше."])
    assert len(made) == 1
    data = bytes(made[0].stdin.data)
    assert len(data) % 2 == 0 and data.endswith(b"\x10\x20")  # вторая фраза не сдвинута на байт


def test_busy_voice_out_is_retried(players):
    made, _ = players
    session = FakeSession(FakeResponse(503), FakeResponse(503), FakeResponse(200, chunks=[b"\x00\x01"]))
    speak_all(session, ["Привет."])
    assert len(session.requests) == 3 and bytes(made[0].stdin.data) == b"\x00\x01"


def test_voice_out_timeout_and_refused_do_not_raise(players):
    session = FakeSession(TimeoutError(), aiohttp.ClientConnectionError("refused"))
    speak_all(session, ["Раз.", "Два."])  # не бросает


def test_voice_out_error_status_skips_phrase(players):
    made, _ = players
    session = FakeSession(FakeResponse(500, body="boom"), FakeResponse(200, chunks=[b"\x01\x00"]))
    speak_all(session, ["Раз.", "Два."])
    assert bytes(made[0].stdin.data) == b"\x01\x00"


def test_hanging_pacat_is_killed_on_finish(players, monkeypatch):
    made, plan = players
    plan.append(FakeProc(hang=True))
    monkeypatch.setattr(core.Speaker, "FINISH_TIMEOUT_S", 0.05)
    speak_all(FakeSession(FakeResponse(200, chunks=[b"\x00\x00"])), ["Фраза."])
    assert made[0].killed


def test_cancel_kills_player_and_stops_speaking(players):
    made, _ = players
    sp = core.Speaker(FakeSession(FakeResponse(200, chunks=[b"\x00\x00"])))

    async def go():
        await sp.speak("Раз.", {"_t0": 0})
        await sp.cancel()
        await sp.speak("Два.", {"_t0": 0})  # после отмены — тишина, в сеть не ходим

    asyncio.run(go())
    assert made[0].killed and len(made) == 1


def test_empty_after_cleaning_is_not_sent(players):
    session = FakeSession()
    speak_all(session, ["😀", "**", "[smiles]"])
    assert session.requests == []
