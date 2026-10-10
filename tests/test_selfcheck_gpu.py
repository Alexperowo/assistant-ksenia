"""Самопроверка: движок, который молча ушёл на процессор (драйвер не был готов при загрузке), — проблема
с перезапуском службы; пока мозг загружается — не тревога."""
import asyncio

from tools import selfcheck


def run_check(monkeypatch, gpu_pids, brain_health=200):
    pids = {"ksenia-brain": "101", "ksenia-voice-out": "202", "ksenia-judge": "303"}

    async def fake_run(*argv):
        if argv[:2] == ("systemctl", "--user") and argv[2] == "is-active":
            return 0, "active"
        if argv[:2] == ("systemctl", "--user") and argv[2] == "show":
            return 0, pids[argv[-1]]
        if argv[0] == "nvidia-smi" and argv[1].startswith("--query-compute-apps"):
            return 0, "\n".join(gpu_pids)
        if argv[0] == "pactl":
            return 0, "bluez_output.x"
        return 0, ""

    async def fake_http(session, url, headers=None):
        return brain_health

    monkeypatch.setattr(selfcheck, "_run", fake_run)
    monkeypatch.setattr(selfcheck, "_http", fake_http)
    return asyncio.run(selfcheck.call("self_check", {}, None))


def test_all_on_gpu_is_fine(monkeypatch):
    r = run_check(monkeypatch, ["101", "202", "303"])
    assert r["restart"] == [] and not any("видеокарт" in p for p in r["problems"])


def test_brain_on_cpu_is_reported_and_restarted(monkeypatch):
    r = run_check(monkeypatch, ["202", "303"])
    assert r["restart"] == ["ksenia-brain"]
    assert any("мозг работает без видеокарты" in p for p in r["problems"])


def test_loading_brain_is_not_an_alarm(monkeypatch):
    r = run_check(monkeypatch, ["202", "303"], brain_health=503)
    assert r["restart"] == []
