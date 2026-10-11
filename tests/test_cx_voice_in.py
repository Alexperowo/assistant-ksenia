"""Профили Bluetooth и HTTP-статус слуха без pactl, микрофона и звуковых служб."""
import asyncio
import json
from types import SimpleNamespace

import pytest

voice_in = pytest.importorskip("voice_in")

CARD = "bluez_card.88_92_CC_D8_81_0F"


def response_json(response):
    return json.loads(response.text)


def patch_to_thread(monkeypatch):
    async def direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(voice_in.asyncio, "to_thread", direct)


def test_bt_node_selects_le_output_and_ignores_monitor(monkeypatch):
    outputs = (
        "210\tbluez_output.88:92:CC:D8:81:0F\tPipeWire\tRUNNING\n"
        "211\tbluez_output.88_92_CC_D8_81_0F.1.monitor\tPipeWire\tSUSPENDED\n"
        "212\tbluez_output.88_92_CC_D8_81_0F.1\tPipeWire\tSUSPENDED\n"
    )
    monkeypatch.setattr(voice_in, "sh", lambda *args: outputs)
    monkeypatch.setattr(voice_in, "card_profile", lambda card: "bap-duplex")

    assert voice_in.bt_node(CARD, "sinks") == "bluez_output.88_92_CC_D8_81_0F.1"


def test_handle_status_reports_connected_duplex_and_busy(monkeypatch):
    patch_to_thread(monkeypatch)

    class Lock:
        def locked(self):
            return True

    monkeypatch.setattr(voice_in, "ear", SimpleNamespace(lock=Lock(), streaming=False))
    monkeypatch.setattr(voice_in, "find_bt_card", lambda: CARD)
    monkeypatch.setattr(voice_in, "card_profile", lambda card: "bap-duplex")
    monkeypatch.setattr(voice_in, "find_source", lambda card: "bluez_input.LE")
    monkeypatch.setattr(voice_in, "card_profiles", lambda card: ["a2dp-ldac", "bap-duplex"])

    result = response_json(asyncio.run(voice_in.handle_status(None)))

    assert result == {"busy": True, "bt_card": CARD, "profile": "bap-duplex", "source": "bluez_input.LE",
                      "duplex": True}


def test_handle_status_returns_empty_snapshot_if_pactl_fails(monkeypatch):
    patch_to_thread(monkeypatch)

    class Lock:
        def locked(self):
            return False

    def fail():
        raise OSError("pactl недоступен")

    monkeypatch.setattr(voice_in, "ear", SimpleNamespace(lock=Lock(), streaming=False))
    monkeypatch.setattr(voice_in, "find_bt_card", fail)

    result = response_json(asyncio.run(voice_in.handle_status(None)))

    assert result == {"busy": False, "bt_card": None, "profile": None, "source": None, "duplex": False}


def _listen_setup(monkeypatch, *, profile, profiles, source):
    patch_to_thread(monkeypatch)
    changes, restores, recordings = [], [], []

    class Ear:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.streaming = False
            self.vp = None

        async def record_utterance(self, node, sink, client_gone):
            recordings.append((node, sink))
            return b"pcm", {"audio_s": 0.2}

        def transcribe_sure(self, pcm):
            return "Привет.", {"mean": 0.0, "weak": []}

    ear = Ear()
    monkeypatch.setattr(voice_in, "ear", ear)
    monkeypatch.setitem(voice_in.CONFIG, "bluetooth", True)
    monkeypatch.setitem(voice_in.CONFIG, "beep_sink", None)
    monkeypatch.setitem(voice_in.CONFIG, "le_settle_s", 0)
    monkeypatch.setattr(voice_in, "find_bt_card", lambda: CARD)
    monkeypatch.setattr(voice_in, "card_profile", lambda card: profile)
    monkeypatch.setattr(voice_in, "card_profiles", lambda card: list(profiles))
    monkeypatch.setattr(voice_in, "find_source", lambda card: source)
    monkeypatch.setattr(voice_in, "bt_node", lambda card, kind: "bluez_output.LE")

    async def set_profile(card, name):
        changes.append((card, name))

    async def restore_a2dp(card, name):
        restores.append((card, name))
        return 0.03

    async def infer(fn, *args):
        return fn(*args)

    monkeypatch.setattr(voice_in, "set_profile", set_profile)
    monkeypatch.setattr(voice_in, "restore_a2dp", restore_a2dp)
    monkeypatch.setattr(voice_in, "infer", infer)
    monkeypatch.setattr(voice_in.mood, "hint", lambda *args: None)

    class Transport:
        def is_closing(self):
            return False

    request = SimpleNamespace(transport=Transport())
    return request, changes, restores, recordings


def test_handle_listen_switches_to_le_and_restores_a2dp_after_recording(monkeypatch):
    request, changes, restores, recordings = _listen_setup(
        monkeypatch, profile="a2dp-ldac", profiles=["a2dp-ldac", "bap-duplex"], source="bluez_input.LE")

    result = response_json(asyncio.run(voice_in.handle_listen(request)))

    assert changes == [(CARD, "bap-duplex")]
    assert recordings == [("bluez_input.LE", "bluez_output.LE")]
    assert restores == [(CARD, "a2dp-ldac")]
    assert result["text"] == "Привет."
    assert result["timings"]["mode"] == "le"
    assert result["timings"]["a2dp_ready_s"] == 0.03


def test_handle_listen_picks_best_a2dp_restore_when_already_in_hfp(monkeypatch):
    request, changes, restores, _ = _listen_setup(
        monkeypatch, profile="headset-head-unit", profiles=["a2dp-aac", "a2dp-ldac"], source="bluez_input.HFP")

    asyncio.run(voice_in.handle_listen(request))

    assert changes == []
    assert restores == [(CARD, "a2dp-ldac")]


def test_handle_listen_restores_a2dp_when_le_profile_has_no_microphone(monkeypatch):
    request, changes, restores, _ = _listen_setup(
        monkeypatch, profile="a2dp-ldac", profiles=["a2dp-ldac", "bap-duplex"], source=None)

    result = asyncio.run(voice_in.handle_listen(request))

    assert result.status == 503
    assert changes == [(CARD, "bap-duplex")]
    assert restores == [(CARD, "a2dp-ldac")]


def test_stream_restores_a2dp_only_when_requested_for_music_back(monkeypatch):
    patch_to_thread(monkeypatch)
    changes, restores = [], []

    class Mic:
        async def readexactly(self, n):
            raise asyncio.IncompleteReadError(b"", n)

    class Recorder:
        def __init__(self):
            self.returncode = None
            self.stdout = Mic()

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    class WS:
        def __init__(self):
            self.closed = False
            self.events = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(60)
            raise StopAsyncIteration

        async def send_json(self, event):
            self.events.append(event)

        async def close(self):
            self.closed = True

    async def set_profile(card, profile):
        changes.append((card, profile))

    async def restore_a2dp(card, profile):
        restores.append((card, profile))
        return 0.03

    async def create_subprocess_exec(*args, **kwargs):
        assert args[0] == "parec"
        return Recorder()

    monkeypatch.setattr(voice_in, "find_bt_card", lambda: CARD)
    monkeypatch.setattr(voice_in, "card_profiles", lambda card: ["a2dp-ldac", "bap-duplex"])
    monkeypatch.setattr(voice_in, "card_profile", lambda card: "a2dp-ldac")
    monkeypatch.setattr(voice_in, "find_source", lambda card: "bluez_input.LE")
    monkeypatch.setattr(voice_in, "bt_node", lambda card, kind: "bluez_output.LE")
    monkeypatch.setattr(voice_in, "set_profile", set_profile)
    monkeypatch.setattr(voice_in, "restore_a2dp", restore_a2dp)
    monkeypatch.setattr(voice_in.asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(voice_in, "headset", None)
    monkeypatch.setitem(voice_in.CONFIG, "bluetooth", True)
    monkeypatch.setitem(voice_in.CONFIG, "beep", False)
    monkeypatch.setitem(voice_in.CONFIG, "le_settle_s", 0)

    ws = WS()
    asyncio.run(voice_in._stream(ws, push=False, back=True))

    assert changes == [(CARD, "bap-duplex")]
    assert restores == [(CARD, "a2dp-ldac")]
    assert [event.get("reason") for event in ws.events if event.get("type") == "error"] == ["mic_lost"]
