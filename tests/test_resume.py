"""После сна компьютера: зависшая видеокарта — сказать честно; зависший слух и службы — перезапустить."""
import asyncio

import pytest

import core


def test_sleep_is_wall_clock_jump_without_monotonic():
    assert core.slept_s(1000, 50, 1610, 60) == 600
    assert core.slept_s(1000, 50, 1010, 60) == 0


@pytest.fixture
def env(monkeypatch, _no_real_services):
    said = []

    async def notice(text, output=None):
        said.append(text)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(core, "say_notice", notice)
    monkeypatch.setattr(core.daily, "notify", lambda t: None)
    monkeypatch.setattr(core.asyncio, "sleep", no_sleep)

    async def settled(timeout_s=150):
        pass

    monkeypatch.setattr(core, "units_settled", settled)
    return said, _no_real_services


def test_hung_gpu_is_reported(env, monkeypatch):
    said, restarted = env

    class R:
        returncode = 1

    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(core.shutil, "which", lambda x: "/usr/bin/nvidia-smi")
    asyncio.run(core.after_resume_check())
    assert said and "перезагрузка" in said[0] and restarted == []


def test_hung_hearing_and_cpu_brain_are_restarted(env, monkeypatch):
    said, restarted = env
    monkeypatch.setattr(core.shutil, "which", lambda x: None)
    calls = []

    async def check(name, args, session):
        calls.append(1)
        if len(calls) == 1:
            return {"problems": ["мозг работает без видеокарты"], "restart": ["ksenia-brain"]}
        return {"problems": [], "restart": []}

    class Sess:
        def get(self, *a, **k):
            raise core.aiohttp.ClientConnectionError()

    monkeypatch.setattr(core.selfcheck, "call", check)
    monkeypatch.setattr(core.ks, "session", Sess(), raising=False)
    asyncio.run(core.after_resume_check())
    assert restarted == ["ksenia-voice-in", "ksenia-brain"] and said == []
