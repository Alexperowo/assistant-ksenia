"""Сторож сети браузера: только публичные адреса, на каждом соединении, без повторного DNS."""
import asyncio
import socket

import pytest
from aiohttp import web

from tools import net_guard


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("77.88.55.88", True), ("2a00:1450:4010::65", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.0.14", False), ("172.16.5.5", False),
    ("100.64.0.1", False), ("169.254.169.254", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fd00::1", False), ("fe80::1", False), ("::ffff:127.0.0.1", False), ("::ffff:192.168.1.1", False),
])
def test_ip_is_public(ip, public):
    assert net_guard.ip_is_public(ip) is public


@pytest.mark.parametrize("host,local", [
    ("localhost", True), ("router", True), ("nas.local", True), ("printer.home.arpa", True), ("x.localhost", True),
    ("fritz.box.lan", True), ("", True), ("vk.ru", False), ("ya.ru.", False),
])
def test_name_is_local(host, local):
    assert net_guard.name_is_local(host) is local


def fake_dns(monkeypatch, answers):
    calls = []

    async def getaddrinfo(self, host, port, **kw):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers.pop(0)]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)
    return calls


def test_any_local_answer_refuses(monkeypatch):
    fake_dns(monkeypatch, [["93.184.216.34", "127.0.0.1"]])  # rebinding через две записи
    assert asyncio.run(net_guard.resolve_public("evil.example", 80)) is None


def test_public_answer_is_used(monkeypatch):
    calls = fake_dns(monkeypatch, [["93.184.216.34"]])
    assert asyncio.run(net_guard.resolve_public("example.com", 443)) == "93.184.216.34" and calls == ["example.com"]


def test_literal_and_local_names_skip_dns(monkeypatch):
    calls = fake_dns(monkeypatch, [])
    assert asyncio.run(net_guard.resolve_public("192.168.0.1", 80)) is None
    assert asyncio.run(net_guard.resolve_public("[::1]", 80)) is None
    assert asyncio.run(net_guard.resolve_public("router", 80)) is None
    assert calls == []


@pytest.fixture
def proxy(monkeypatch):
    """Прокси, в котором «интернет» — 127.0.0.1, а «домашняя сеть» — 127.0.0.2."""
    monkeypatch.setattr(net_guard, "ip_is_public", lambda ip: str(ip) == "127.0.0.1")
    monkeypatch.setattr(net_guard, "_server", {"srv": None, "port": None})
    hits = []

    async def echo(req):
        hits.append((req.host, req.path, dict(req.headers)))
        return web.Response(text=f"ok {req.path}")

    async def setup():
        app = web.Application()
        app.add_routes([web.get("/{p:.*}", echo)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, port, await net_guard.start()

    return setup, hits


async def raw(port, data):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(data)
    await w.drain()
    out = await asyncio.wait_for(r.read(), 5)
    w.close()
    return out.decode("latin-1")


def test_http_is_forwarded_with_one_request_per_connection(proxy):
    setup, hits = proxy

    async def go():
        runner, port, pport = await setup()
        try:
            return await raw(pport, f"GET http://127.0.0.1:{port}/a?b=1 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                                    f"Proxy-Connection: keep-alive\r\n\r\n".encode())
        finally:
            await runner.cleanup()

    out = asyncio.run(go())
    assert out.startswith("HTTP/1.1 200") and out.endswith("ok /a")
    headers = {k.lower(): v for k, v in hits[0][2].items()}
    assert "proxy-connection" not in headers and headers["connection"] == "close"


def test_http_to_local_address_is_refused(proxy):
    setup, hits = proxy

    async def go():
        runner, port, pport = await setup()
        try:
            return await raw(pport, b"GET http://127.0.0.2:8080/admin HTTP/1.1\r\nHost: 127.0.0.2\r\n\r\n")
        finally:
            await runner.cleanup()

    assert asyncio.run(go()).startswith("HTTP/1.1 403") and hits == []


def test_connect_tunnel_and_refusal(proxy):
    setup, hits = proxy

    async def go():
        runner, port, pport = await setup()
        try:
            refused = await raw(pport, b"CONNECT 192.168.0.1:443 HTTP/1.1\r\nHost: 192.168.0.1:443\r\n\r\n")
            r, w = await asyncio.open_connection("127.0.0.1", pport)
            w.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            w.write(f"GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
            tunneled = (await asyncio.wait_for(r.read(), 5)).decode()
            w.close()
            return refused, tunneled
        finally:
            await runner.cleanup()

    refused, tunneled = asyncio.run(go())
    assert refused.startswith("HTTP/1.1 403") and tunneled.endswith("ok /tunnel")


def test_garbage_request_is_closed(proxy):
    setup, _ = proxy

    async def go():
        runner, port, pport = await setup()
        try:
            return await raw(pport, b"\x16\x03\x01garbage\r\n\r\n")
        finally:
            await runner.cleanup()

    out = asyncio.run(go())
    assert out == "" or out.startswith("HTTP/1.1 403")


def test_browser_proxy_disables_loopback_bypass():
    assert net_guard.browser_proxy(1234) == {"server": "http://127.0.0.1:1234", "bypass": "<-loopback>"}
