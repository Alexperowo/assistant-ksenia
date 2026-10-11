"""Проверки переходов живого режима, которых не было в старых тестах."""
import asyncio
import json
import time
from types import SimpleNamespace

import core


def test_doze_and_touch_wake_update_state_and_speak(monkeypatch):
    events, said = [], []
    monkeypatch.setattr(core.hub, "emit", events.append)

    async def notice(text, output=None):
        said.append((text, output))

    monkeypatch.setattr(core, "say_notice", notice)
    live = core.LiveConversation()
    live.output = "client"

    live.doze("попрощались")
    assert live.dozing is True
    assert events[-1] == {"type": "state", "state": "idle", "where": "pc"}

    asyncio.run(live.wake())
    assert live.dozing is False
    assert events[-1] == {"type": "state", "state": "listening", "where": "pc"}
    assert said and said[0][0] in {"Да?", "Слушаю.", "Да, я тут."}
    assert said[0][1] == "client"


def _goodbye(monkeypatch, *, music_back):
    live = core.LiveConversation()
    live.music_back = music_back
    live.turns = 0
    live.dozing = False
    spoken = []

    async def turn(text, timings, speaker=None, resume=False, output="local"):
        spoken.append(text)

    async def noop(*args, **kwargs):
        return None

    async def decide(*args, **kwargs):
        return core.live_intent.Decision("goodbye", True)

    monkeypatch.setitem(core.CONFIG, "live_doze", True)
    monkeypatch.setitem(core.voicectl.STATE, "enrolling", 0)
    monkeypatch.setattr(core, "turn", turn)
    monkeypatch.setattr(core, "deliver_waiting", noop)
    monkeypatch.setattr(core.music, "duck", noop)
    monkeypatch.setattr(core.live_intent, "decide", decide)
    monkeypatch.setattr(core.hub, "emit", lambda event: None)

    ended = asyncio.run(live.on_utterance({"type": "utterance", "text": "Пока.", "timings": {}}))
    return live, ended, spoken


def test_goodbye_doze_waits_for_name_when_not_returning_to_music(monkeypatch):
    live, ended, spoken = _goodbye(monkeypatch, music_back=False)

    assert spoken == ["Пока."]
    assert ended is False
    assert live.dozing is True


def test_goodbye_from_music_ends_stream_without_dozing(monkeypatch):
    live, ended, spoken = _goodbye(monkeypatch, music_back=True)

    assert spoken == ["Пока."]
    assert ended is True
    assert live.dozing is False


def test_start_talk_marks_music_route_for_profile_restore(monkeypatch):
    captured = []

    async def stop():
        return False

    async def route():
        return "music"

    async def run(self):
        captured.append((self.music_back, self.start_dozing))

    monkeypatch.setattr(core, "stop_conversation", stop)
    monkeypatch.setattr(core, "live_route", route)
    monkeypatch.setattr(core.LiveConversation, "run", run)
    monkeypatch.setattr(core.conv, "task", None)
    monkeypatch.setattr(core.conv, "_runner", None, raising=False)
    monkeypatch.setattr(core.live, "music_back", False)
    monkeypatch.setitem(core.CONFIG, "live_mode", True)

    async def go():
        runner = await core.start_talk()
        await core.conv.task
        return runner

    runner = asyncio.run(go())

    assert runner is core.live
    assert captured == [(True, False)]


def test_live_stream_requests_a2dp_restore_for_music_back(monkeypatch):
    urls = []

    class Message:
        type = core.aiohttp.WSMsgType.TEXT
        data = json.dumps({"type": "utterance", "text": "Пока."})

    class WS:
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def receive_json(self, timeout=None):
            return {"type": "ready"}

        async def send_json(self, data):
            pass

        async def receive(self, timeout=None):
            return Message()

        async def close(self):
            self.closed = True

    class Session:
        def ws_connect(self, url, heartbeat=None):
            urls.append(url)
            return WS()

    live = core.LiveConversation()
    live.music_back = True

    async def done(_ev):
        return True

    monkeypatch.setattr(live, "on_utterance", done)
    monkeypatch.setattr(core.ks, "session", Session())

    asyncio.run(live.run())

    assert urls and urls[0].endswith("/stream?restore=1")


def test_button_state_reports_live_dozing_only_for_active_live_runner(monkeypatch):
    class FakeLock:
        def __init__(self, locked):
            self.value = locked

        def locked(self):
            return self.value

    monkeypatch.setattr(core.ks, "lock", FakeLock(False))
    monkeypatch.setattr(core.ks, "speaker", SimpleNamespace(cancelled=False, _play_end=time.time() + 5))
    monkeypatch.setattr(core.conv, "active", lambda: True)
    monkeypatch.setattr(core.conv, "_runner", core.live, raising=False)
    monkeypatch.setattr(core.live, "dozing", True, raising=False)
    monkeypatch.setattr(core.music, "status", lambda: "playing")

    assert core.button_state() == {"speaking": True, "conversation": True, "music": "playing", "dozing": True}

    monkeypatch.setattr(core.ks, "speaker", SimpleNamespace(cancelled=True, _play_end=time.time() + 5))
    monkeypatch.setattr(core.conv, "active", lambda: False)
    monkeypatch.setattr(core.conv, "_runner", core.conv, raising=False)
    assert core.button_state() == {"speaking": False, "conversation": False, "music": "playing", "dozing": False}


def test_live_route_falls_back_when_status_request_fails(monkeypatch):
    class Session:
        def get(self, *args, **kwargs):
            raise OSError("voice-in недоступен")

    monkeypatch.setattr(core.ks, "session", Session())

    assert asyncio.run(core.live_route()) is None
