"""IPC mpv: ответ на команду не путается с событиями."""
import asyncio
import json
import os
import tempfile

from tools import music


def run_with_fake_mpv(monkeypatch, reply_lines):
    d = tempfile.mkdtemp()
    sock = os.path.join(d, "mpv.sock")
    monkeypatch.setattr(music, "SOCK", sock)
    got = []

    async def handle(r, w):
        req = json.loads(await r.readline())
        got.append(req)
        for line in reply_lines(req):
            w.write((json.dumps(line) + "\n").encode())
        await w.drain()
        w.close()

    async def go():
        server = await asyncio.start_unix_server(handle, path=sock)
        async with server:
            return await music._ipc("get_property", "playlist-pos")

    return asyncio.run(go()), got


def test_event_before_reply_is_skipped(monkeypatch):
    res, got = run_with_fake_mpv(monkeypatch, lambda req: [
        {"event": "metadata-update"},
        {"event": "playback-restart"},
        {"data": 3, "error": "success", "request_id": req["request_id"]},
    ])
    assert res["data"] == 3
    assert got[0]["command"] == ["get_property", "playlist-pos"]


def test_reply_for_other_request_is_skipped(monkeypatch):
    res, _ = run_with_fake_mpv(monkeypatch, lambda req: [
        {"data": 99, "error": "success", "request_id": req["request_id"] + 1000},
        {"data": 4, "error": "success", "request_id": req["request_id"]},
    ])
    assert res["data"] == 4


def test_no_reply_returns_none(monkeypatch):
    res, _ = run_with_fake_mpv(monkeypatch, lambda req: [{"event": "idle"}])
    assert res is None


def test_no_socket_returns_none(monkeypatch):
    monkeypatch.setattr(music, "SOCK", "/nonexistent/ksenia-mpv.sock")
    assert asyncio.run(music._ipc("stop")) is None
