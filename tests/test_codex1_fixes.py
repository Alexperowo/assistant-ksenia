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
    monkeypatch.setattr(core.ks, "last_turn_t", core.time.time(), raising=False)  # только что говорили — без возврата по тишине

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


def _headset_loop(monkeypatch, starts, last_turn_ago, muted=False, client_ago=1000):
    import time as t
    monkeypatch.setitem(core.CONFIG, "live_doze", True)
    monkeypatch.setitem(core.CONFIG, "headset_poll_s", 0)
    monkeypatch.setattr(core.ks, "last_turn_t", t.time() - last_turn_ago, raising=False)
    monkeypatch.setattr(core.ks, "last_client_t", t.time() - client_ago, raising=False)
    monkeypatch.setattr(core.conv, "active", lambda: False)
    monkeypatch.setattr(core.live, "auto_failed", False, raising=False)

    async def fake_start(doze=False, source=None):
        starts.append(doze)
        core.live.user_muted = muted  # как в жизни: старт не трогает «сам попросил не слушать»
    monkeypatch.setattr(core, "start_talk", fake_start)

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
        task = asyncio.create_task(core.headset_auto_live())
        for _ in range(30):
            await asyncio.sleep(0.01)
        task.cancel()
    asyncio.run(go())


def test_doze_comes_back_after_tablet_closed_it(monkeypatch):
    """2026-10-13: после разговора с планшета «Ксения, который час?» в наушниках встречала тишина."""
    starts = []
    _headset_loop(monkeypatch, starts, last_turn_ago=120)
    assert len(starts) >= 2  # подключение — и возврат в «жду имени», когда тихо


def test_no_doze_back_if_he_asked_not_to_listen(monkeypatch):
    starts = []
    _headset_loop(monkeypatch, starts, last_turn_ago=120, muted=True)
    assert len(starts) == 1  # только при подключении


def test_no_doze_back_while_talking_via_tablet(monkeypatch):
    starts = []
    _headset_loop(monkeypatch, starts, last_turn_ago=120, client_ago=5)
    assert len(starts) == 1
