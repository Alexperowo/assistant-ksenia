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
