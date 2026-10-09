"""Наушники: восстановление канала LE Audio по шагам и режимы — без железа (команды подменены)."""
import asyncio

import headset as hs


def test_info_parses_bearers(monkeypatch):
    async def fake_run(*argv, **kw):
        return 0, "\tConnected: yes\n\tBREDR.Connected: no\n\tLE.Connected: yes\n"
    monkeypatch.setattr(hs, "run", fake_run)
    assert asyncio.run(hs.info("AA")) == {"connected": True, "le": True, "bredr": False}


def make(monkeypatch, probes, reconnect_ok=True, restart_ok=True, profile="bap-duplex"):
    calls, said = [], []
    probes = list(probes)

    async def fake_info(mac):
        return {"connected": True, "le": True, "bredr": False}

    async def fake_profile(mac):
        return profile

    async def fake_probe(mac):
        calls.append("probe")
        return probes.pop(0)

    async def fake_reconnect(mac):
        calls.append("reconnect")
        return reconnect_ok

    async def fake_restart(mac):
        calls.append("restart")
        return restart_ok

    async def fake_sleep(s):
        return None

    async def say(t):
        said.append(t)
    monkeypatch.setattr(hs, "info", fake_info)
    monkeypatch.setattr(hs, "card_profile", fake_profile)
    monkeypatch.setattr(hs, "probe", fake_probe)
    monkeypatch.setattr(hs, "reconnect", fake_reconnect)
    monkeypatch.setattr(hs, "restart_bluetooth", fake_restart)
    monkeypatch.setattr(hs.asyncio, "sleep", fake_sleep)
    h = hs.Headset({"headset_mac": "AA"}, say=say)
    return h, calls, said


def test_healthy_channel_does_nothing(monkeypatch):
    h, calls, said = make(monkeypatch, [(True, "ок")])
    assert asyncio.run(h.check_and_recover())["ok"] and calls == ["probe"] and said == []


def test_reconnect_fixes(monkeypatch):
    h, calls, said = make(monkeypatch, [(False, "сбой"), (True, "ок")])
    res = asyncio.run(h.check_and_recover())
    assert res == {"ok": True, "fixed_by": "переподключение"}
    assert calls == ["probe", "reconnect", "probe"] and said  # Ксения говорит, что починила


def test_restart_bluetooth_when_reconnect_not_enough(monkeypatch):
    h, calls, said = make(monkeypatch, [(False, "сбой"), (False, "сбой"), (True, "ок")])
    res = asyncio.run(h.check_and_recover())
    assert res["fixed_by"] == "перезапуск Bluetooth"
    assert calls == ["probe", "reconnect", "probe", "restart", "probe"]


def test_gives_up_honestly(monkeypatch):
    h, calls, said = make(monkeypatch, [(False, "a"), (False, "b"), (False, "c")])
    res = asyncio.run(h.check_and_recover())
    assert not res["ok"] and "выключить и включить" in res["advice"] and said == []


def test_classic_mode_is_not_probed(monkeypatch):
    h, calls, said = make(monkeypatch, [], profile="a2dp-sink")
    assert asyncio.run(h.check_and_recover())["mode"] == "music" and calls == []
