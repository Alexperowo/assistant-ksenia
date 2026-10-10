"""Шлюз планшета: домашняя сеть, Host, Origin, PIN с ограничением попыток, сессия, проксирование к ядру и слуху."""
import asyncio
import json
import os
import struct

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

import gateway


def wav(seconds=0.5, rate=16000):
    n = int(seconds * rate)
    data = b"\x00\x00" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


class FakeBackends:
    """Ядро и слух: записывают, что пришло; ядро держит WebSocket /client."""

    def __init__(self):
        self.core_calls, self.voice_calls, self.core_ws = [], [], []
        self.heard = "Привет, Ксения"
        self.voice_up = True

    async def say(self, req):
        self.core_calls.append(("/say", await req.json()))
        return web.json_response({"reply": "ок"})

    async def simple(self, req):
        self.core_calls.append((req.path, await req.json()))
        return web.json_response({"ok": True})

    async def client(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.core_ws.append(ws)
        await ws.send_json({"type": "hello", "busy": False,
                            "confirm": {"type": "confirm", "label": "x", "question": "Пишу Диме: привет. Отправить?"}})
        async for _ in ws:
            pass
        return ws

    async def transcribe(self, req):
        body = await req.read()
        self.voice_calls.append(body)
        if not self.voice_up:
            return web.Response(status=500, text="boom")
        return web.json_response({"text": self.heard, "speaker": {"owner": True, "enrolled": True, "confirm_ok": False}})

    def core_app(self):
        app = web.Application()
        app.add_routes([web.post("/say", self.say), web.post("/stop", self.simple), web.post("/notice", self.simple),
                        web.post("/duck", self.simple), web.get("/client", self.client)])
        return app

    def voice_app(self):
        app = web.Application()
        app.add_routes([web.post("/transcribe", self.transcribe)])
        return app


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "pwa"
    data.mkdir()
    (data / "pin").write_text("482913\n")
    (data / "hosts.json").write_text(json.dumps(["192.168.0.14", "ksenia-pc.local"]))
    (data / "ca.crt").write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    return data


def run(env, scenario, **cfg_extra):
    """Поднять поддельные ядро и слух, шлюз и «планшет»-клиент, выполнить сценарий."""
    back = FakeBackends()

    async def go():
        core = TestServer(back.core_app(), host="127.0.0.1")
        voice = TestServer(back.voice_app(), host="127.0.0.1")
        await core.start_server()
        await voice.start_server()
        cfg = {"data_dir": str(env), "core_url": f"http://127.0.0.1:{core.port}",
               "voice_in_url": f"http://127.0.0.1:{voice.port}", "session_days": 180, **cfg_extra}
        gw = gateway.Gateway(cfg)
        client = TestClient(TestServer(gw.app(), host="127.0.0.1"))
        await client.start_server()
        try:
            return await scenario(client, gw, back, core)
        finally:
            await client.close()
            await core.close()
            await voice.close()

    return asyncio.run(go())


def origin(c):
    return {"Origin": f"https://127.0.0.1:{c.port}"}


async def login(c, pin="482913"):
    r = await c.post("/api/login", json={"pin": pin}, headers=origin(c))
    return r, (r.cookies[gateway.COOKIE].value if gateway.COOKIE in r.cookies else None)


def auth(c, token):
    return {**origin(c), "Cookie": f"{gateway.COOKIE}={token}"}


def test_login_sets_long_httponly_secure_cookie(env):
    async def s(c, gw, back, core):
        bad, _ = await login(c, "000000")
        r, token = await login(c)
        cookie = r.cookies[gateway.COOKIE]
        sess = await (await c.get("/api/session", headers={"Cookie": f"{gateway.COOKIE}={token}"})).json()
        return bad.status, r.status, cookie, sess

    bad, ok, cookie, sess = run(env, s)
    assert bad == 403 and ok == 200 and sess["authorized"] is True
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Strict"
    assert int(cookie["max-age"]) == 180 * 86400


def test_pin_attempts_are_limited(env):
    async def s(c, gw, back, core):
        codes = [(await login(c, f"00000{i}"))[0].status for i in range(5)]
        locked, _ = await login(c)  # даже верный код — после 5 ошибок ждать
        return codes, locked.status, locked.headers.get("Retry-After")

    codes, locked, retry = run(env, s)
    assert codes == [403] * 5 and locked == 429 and int(retry) > 0


def test_pin_guard_windows():
    t = [0.0]
    g = gateway.PinGuard("123456", per_ip=2, window=60, global_limit=3, global_window=600, clock=lambda: t[0])
    assert g.check("a", "1") == (False, 0) and g.check("a", "2") == (False, 0)
    assert g.check("a", "123456")[0] is False  # заблокирован
    t[0] = 61
    assert g.check("a", " 123 456 ")[0] is True  # окно прошло; пробелы в коде не мешают
    g.check("b", "x")
    g.check("c", "x")
    g.check("d", "x")
    assert g.check("e", "123456")[0] is False  # перебор с разных адресов — общий предел


def test_sessions_hashed_expire_and_revoke(tmp_path):
    t = [1000.0]
    path = str(tmp_path / "s.json")
    s = gateway.Sessions(path, days=1, clock=lambda: t[0])
    token = s.create("Tab S9")
    assert token not in open(path).read()  # на диске только хэш
    assert gateway.Sessions(path, days=1, clock=lambda: t[0]).valid(token)  # переживает перезапуск
    t[0] += 2 * 86400
    assert not s.valid(token)
    t[0] = 1000.0
    s.revoke(token)
    assert not s.valid(token) and os.stat(path).st_mode & 0o077 == 0


def test_only_home_network(env, monkeypatch):
    monkeypatch.setattr(gateway, "client_ip", lambda request: "8.8.8.8")

    async def s(c, gw, back, core):
        return (await c.get("/")).status, (await login(c))[0].status

    assert run(env, s) == (403, 403)


def test_unknown_host_is_rejected(env):
    async def s(c, gw, back, core):
        return (await c.get("/", headers={"Host": f"rebind.evil.example:{c.port}"})).status

    assert run(env, s) == 421


@pytest.mark.parametrize("hdr", [{}, {"Origin": "https://evil.example"}, {"Origin": "null"}])
def test_post_needs_own_origin(env, hdr):
    async def s(c, gw, back, core):
        return (await c.post("/api/login", json={"pin": "482913"}, headers=hdr)).status

    assert run(env, s) == 403


def test_api_needs_session(env):
    async def s(c, gw, back, core):
        r1 = await c.post("/api/text", json={"text": "привет"}, headers=origin(c))
        r2 = await c.post("/api/text", json={"text": "привет"},
                          headers={**origin(c), "Cookie": f"{gateway.COOKIE}=forged"})
        return r1.status, r2.status, back.core_calls

    assert run(env, s) == (401, 401, [])


def test_static_is_public_with_csp(env):
    async def s(c, gw, back, core):
        r = await c.get("/")
        return r.status, r.headers.get("Content-Security-Policy", ""), r.headers.get("X-Content-Type-Options")

    status, csp, nosniff = run(env, s)
    assert status == 200 and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and nosniff == "nosniff"


def test_utterance_goes_to_voice_in_then_core(env):
    async def s(c, gw, back, core):
        _, token = await login(c)
        body = wav()
        r = await c.post("/api/utterance", data=body, headers={**auth(c, token), "Content-Type": "audio/wav"})
        res = await r.json()
        await asyncio.sleep(0.1)
        return res, back.voice_calls[0] == body, back.core_calls

    res, same_audio, calls = run(env, s)
    assert res == {"text": "Привет, Ксения"} and same_audio
    # чей голос — дальше в ядро: «да» с планшета решает только уверенно узнанный голос Александра
    assert calls == [("/say", {"text": "Привет, Ксения", "output": "client",
                               "speaker": {"owner": True, "enrolled": True, "confirm_ok": False, "source": "tablet"}})]


def test_not_wav_and_voice_down(env):
    async def s(c, gw, back, core):
        _, token = await login(c)
        r1 = await c.post("/api/utterance", data=b"OggS....", headers=auth(c, token))
        back.voice_up = False
        r2 = await c.post("/api/utterance", data=wav(), headers=auth(c, token))
        return r1.status, r2.status, back.core_calls

    assert run(env, s) == (415, 502, [])


def test_silence_is_not_sent_to_core(env):
    async def s(c, gw, back, core):
        back.heard = ""
        _, token = await login(c)
        r = await c.post("/api/utterance", data=wav(), headers=auth(c, token))
        await asyncio.sleep(0.05)
        return await r.json(), back.core_calls

    assert run(env, s) == ({"text": ""}, [])


def test_buttons_stop_and_listening(env):
    async def s(c, gw, back, core):
        _, token = await login(c)
        await c.post("/api/text", json={"text": "да"}, headers=auth(c, token))
        await c.post("/api/stop", json={}, headers=auth(c, token))
        await c.post("/api/listening", json={"on": True}, headers=auth(c, token))
        await asyncio.sleep(0.1)
        return sorted(back.core_calls, key=lambda x: x[0])

    calls = run(env, s)
    assert ("/say", {"text": "да", "output": "client"}) in calls
    assert ("/stop", {}) in calls and ("/duck", {"on": True}) in calls


def test_pin_is_spoken_at_pc_with_rate_limit(env):
    async def s(c, gw, back, core):
        r1 = await c.post("/api/pin/speak", headers=origin(c))
        r2 = await c.post("/api/pin/speak", headers=origin(c))
        return r1.status, r2.status, back.core_calls

    s1, s2, calls = run(env, s)
    assert (s1, s2) == (200, 429)
    assert calls == [("/notice", {"text": "Код для планшета: 4, 8, 2, 9, 1, 3."})]


def test_events_and_audio_are_relayed(env):
    async def s(c, gw, back, core):
        _, token = await login(c)
        for _ in range(50):
            if gw.core_up:
                break
            await asyncio.sleep(0.02)
        ws = await c.ws_connect("/api/ws", headers=auth(c, token))
        first = [await ws.receive_json(), await ws.receive_json()]
        await asyncio.sleep(0.05)
        await back.core_ws[0].send_json({"type": "say", "text": "Привет!"})
        await back.core_ws[0].send_bytes(b"\x01\x02")
        ev = await ws.receive_json()
        audio = await ws.receive()
        await back.core_ws[0].close()  # ядро перезапускается
        down = await ws.receive_json()
        await ws.close()
        return first, ev, audio, down

    first, ev, audio, down = run(env, s)
    assert first[0] == {"type": "link", "core": True}
    assert first[1]["question"] == "Пишу Диме: привет. Отправить?"  # вкладка открыта после вопроса — кнопки видны
    assert ev == {"type": "say", "text": "Привет!"}
    assert audio.type == WSMsgType.BINARY and audio.data == b"\x01\x02"
    assert down == {"type": "link", "core": False}


def test_websocket_needs_session_and_origin(env):
    async def s(c, gw, back, core):
        _, token = await login(c)
        r1 = await c.get("/api/ws", headers={"Origin": f"https://127.0.0.1:{c.port}", "Upgrade": "websocket",
                                             "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                                             "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        r2 = await c.get("/api/ws", headers={"Origin": "https://evil.example", "Cookie": f"{gateway.COOKIE}={token}",
                                             "Upgrade": "websocket", "Connection": "Upgrade",
                                             "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        return r1.status, r2.status

    assert run(env, s) == (401, 403)


def test_logout(env):
    async def s(c, gw, back, core):
        _, token = await login(c)
        await c.post("/api/logout", headers=auth(c, token))
        return (await c.post("/api/text", json={"text": "x"}, headers=auth(c, token))).status

    assert run(env, s) == 401


def test_plain_http_serves_only_ca_and_help(env):
    async def go():
        gw = gateway.Gateway({"data_dir": str(env), "core_url": "http://127.0.0.1:1", "voice_in_url": "http://127.0.0.1:1"})
        async with TestClient(TestServer(gw.plain_app(), host="127.0.0.1")) as c:
            ca = await c.get("/ksenia-ca.crt")
            api = await c.post("/api/login", json={"pin": "482913"})
            return ca.status, ca.headers["Content-Type"], (await ca.read())[:27], api.status

    assert asyncio.run(go()) == (200, "application/x-x509-ca-cert", b"-----BEGIN CERTIFICATE-----", 404)


def test_new_pin_is_six_digits_private(tmp_path):
    pin = gateway.load_pin(str(tmp_path / "pin"))
    assert pin.isdigit() and len(pin) == 6 and os.stat(tmp_path / "pin").st_mode & 0o077 == 0
    assert gateway.load_pin(str(tmp_path / "pin")) == pin


def test_help_page_links_to_https_app(env):
    async def go():
        gw = gateway.Gateway({"data_dir": str(env), "core_url": "http://127.0.0.1:1", "voice_in_url": "http://127.0.0.1:1",
                              "https_port": 18140})
        async with TestClient(TestServer(gw.plain_app(), host="127.0.0.1")) as c:
            known = await (await c.get("/", headers={"Host": "192.168.0.14:18141"})).text()
            unknown = await (await c.get("/", headers={"Host": "evil.example:18141"})).text()
            return known, unknown

    known, unknown = asyncio.run(go())
    assert 'href="https://192.168.0.14:18140/"' in known
    assert "evil.example" not in unknown and "https://" in unknown
