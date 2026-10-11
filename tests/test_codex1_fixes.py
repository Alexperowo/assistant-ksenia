"""Исправления по анализу Codex №1 (docs/REVIEW-CODEX-1.md)."""
import asyncio

import core
from tools import daily, settings


def test_remind_both_in_minutes_and_at_is_refused():
    r = asyncio.run(daily.call("remind_set", {"text": "чай", "in_minutes": 5, "at": "18:00"}, None))
    assert not r["ok"] and "одно" in r["error"]


def _volume_env(monkeypatch, before, after, rc=0):
    seq = iter([(before, False), (after, False)])

    async def vol():
        return next(seq)

    async def run(*argv, timeout=15):
        return rc, ""
    monkeypatch.setattr(settings, "_volume", vol)
    monkeypatch.setattr(settings, "_run", run)


def test_volume_up_that_changed_nothing_is_not_success(monkeypatch):
    _volume_env(monkeypatch, 50, 50)
    r = asyncio.run(settings.call("setting", {"action": "volume_up"}, None))
    assert not r["ok"]


def test_volume_up_ok(monkeypatch):
    _volume_env(monkeypatch, 50, 60)
    assert asyncio.run(settings.call("setting", {"action": "volume_up"}, None))["ok"]


def test_volume_up_at_max_is_honest(monkeypatch):
    _volume_env(monkeypatch, 150, 150)
    r = asyncio.run(settings.call("setting", {"action": "volume_up"}, None))
    assert r["ok"] and "самая большая" in r["note"]


def test_auto_doze_retries_after_busy_hearing(monkeypatch):
    """Слух был занят, когда наушники подключились, — «жду имени» включается при следующем опросе, а не
    после переподключения наушников."""
    monkeypatch.setitem(core.CONFIG, "live_doze", True)
    monkeypatch.setitem(core.CONFIG, "headset_poll_s", 0)
    starts = []

    async def fake_start(doze=False, source=None):
        starts.append(doze)
        core.live.auto_failed = len(starts) == 1  # первая попытка сорвалась
    monkeypatch.setattr(core, "start_talk", fake_start)
    monkeypatch.setattr(core.conv, "active", lambda: False)
    monkeypatch.setattr(core.live, "auto_failed", False, raising=False)

    class R:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self, content_type=None):
            return {"connected": True, "mode": "talk"}

    class S:
        def get(self, *a, **k):
            return R()
    monkeypatch.setattr(core.ks, "session", S())

    async def go():
        t = asyncio.create_task(core.headset_auto_live())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(starts) >= 2:
                break
        t.cancel()
    asyncio.run(go())
    assert starts[:2] == [True, True]
    assert len(starts) == 2  # после удачной — больше не дёргает
