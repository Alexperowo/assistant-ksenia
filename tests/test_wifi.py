"""Wi-Fi: «:» в имени сети, «unknown» — не повод откатывать рабочую сеть, пароль — не в историю и журнал."""
import asyncio
import json

import core
from tools import settings


def test_colon_in_ssid():
    assert settings._fields(r"yes:My\:Net:70") == ["yes", "My:Net", "70"]


def test_unknown_connectivity_but_connected_is_online(monkeypatch):
    async def run(*argv, timeout=15):
        return 0, "unknown" if "connectivity" in argv else "connected"
    monkeypatch.setattr(settings, "_run", run)
    assert asyncio.run(settings._online(timeout=3)) is True


def test_password_redacted_for_history():
    calls = [{"id": "1", "function": {"name": "setting",
                                      "arguments": json.dumps({"action": "wifi_connect", "password": "secret1"})}}]
    red = core.redact_calls(calls)
    assert "secret1" not in json.dumps(red) and "secret1" in calls[0]["function"]["arguments"]


def test_volume_never_below_floor_and_mute_pauses_music(monkeypatch):
    ran, paused = [], []

    async def run(*argv, timeout=15):
        ran.append(argv)
        return 0, "Volume: 0.10"

    async def music_call(name, args, session):
        paused.append(args["action"])
        return {"ok": True}

    from tools import music
    monkeypatch.setattr(settings, "_run", run)
    monkeypatch.setattr(music, "call", music_call)
    r = asyncio.run(settings.call("setting", {"action": "volume_set", "value": "0"}, None))
    assert ("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.10") in ran and "note" in r
    r = asyncio.run(settings.call("setting", {"action": "mute"}, None))
    assert paused == ["pause"] and not any("set-mute" in a and "1" in a for a in ran)
