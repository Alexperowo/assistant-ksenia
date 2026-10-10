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
        if argv[0] == "curl":
            return 0, '{"busy": false}'
        if argv[0] == "nvidia-smi":
            return 0, "0, 14000, 16000, 50"
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


def test_hung_hearing_and_dead_gpus_are_problems(monkeypatch):
    async def fake_run(*argv):
        if argv[:3] == ("systemctl", "--user", "is-active"):
            return 0, "active"
        if argv[:3] == ("systemctl", "--user", "show"):
            return 0, "0"
        if argv[0] == "curl":
            return -1, "не ответил"
        if argv[0] == "nvidia-smi":
            return -1, "не ответил"
        return 0, "bluez_output.x"

    async def fake_http(session, url, headers=None):
        return 200

    monkeypatch.setattr(selfcheck, "_run", fake_run)
    monkeypatch.setattr(selfcheck, "_http", fake_http)
    r = asyncio.run(selfcheck.call("self_check", {}, None))
    assert any("слух" in p and "не отвечает" in p for p in r["problems"])
    assert any("видеокарты не отвечают" in p for p in r["problems"]) and "ksenia-voice-in" in r["restart"]


def test_gpu_xid_in_kernel_log_is_a_problem(monkeypatch):
    async def fake_run(*argv):
        if argv[0] == "journalctl":
            return 0, ("kernel: NVRM: Xid (PCI:0000:01:00): 79, pid=1, name=llama-server, GPU has fallen off the bus.\n"
                       "kernel: NVRM: Xid (PCI:0000:01:00): 154, GPU recovery action changed")
        if argv[:3] == ("systemctl", "--user", "is-active"):
            return 0, "active"
        if argv[:3] == ("systemctl", "--user", "show"):
            return 0, "0"
        if argv[0] == "curl":
            return 0, '{"busy": false}'
        if argv[0] == "nvidia-smi":
            return 0, "0, 14000, 16000, 50"
        return 0, "bluez_output.x"

    async def fake_http(session, url, headers=None):
        return 200

    monkeypatch.setattr(selfcheck, "_run", fake_run)
    monkeypatch.setattr(selfcheck, "_http", fake_http)
    r = asyncio.run(selfcheck.call("self_check", {}, None))
    assert any("Xid 79" in p and "отвалилась" in p for p in r["problems"])
