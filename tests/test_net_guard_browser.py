"""Сторож сети с настоящим Chromium (необязательный тест: нужен Playwright и браузер).

Запуск: KSENIA_TEST_CHROMIUM=/путь/к/chrome python -m pytest tests/test_net_guard_browser.py
(у Ксении браузеры лежат в data/playwright). Без переменной тест пропускается.
"""
import asyncio
import os

import pytest
from aiohttp import web

from tools import net_guard

CHROME = os.environ.get("KSENIA_TEST_CHROMIUM")
pytestmark = pytest.mark.skipif(not CHROME, reason="нужен KSENIA_TEST_CHROMIUM")


def test_redirect_subresource_and_direct_local_are_blocked(monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(net_guard, "ip_is_public", lambda ip: str(ip) == "127.0.0.1")
    monkeypatch.setattr(net_guard, "_server", {"srv": None, "port": None})
    hits = []

    async def go():
        async def start(req):
            return web.HTTPFound("http://127.0.0.2:18992/secret")

        async def page(req):
            return web.Response(text="<p>public</p><img src='http://127.0.0.2:18992/img'>", content_type="text/html")

        async def secret(req):
            hits.append(req.path)
            return web.Response(text="secret")

        a, b = web.Application(), web.Application()
        a.add_routes([web.get("/start", start), web.get("/page", page)])
        b.add_routes([web.get("/secret", secret), web.get("/img", secret)])
        runners = []
        for app, host, port in ((a, "127.0.0.1", 18991), (b, "127.0.0.2", 18992)):
            r = web.AppRunner(app)
            await r.setup()
            await web.TCPSite(r, host, port).start()
            runners.append(r)
        port = await net_guard.start()
        async with playwright.async_playwright() as p:
            br = await p.chromium.launch(executable_path=CHROME, proxy=net_guard.browser_proxy(port))
            pg = await br.new_page()
            ok = await pg.goto("http://127.0.0.1:18991/page")
            await pg.wait_for_timeout(300)
            redirected = await pg.goto("http://127.0.0.1:18991/start")
            direct = await pg.goto("http://127.0.0.2:18992/secret")
            await br.close()
        for r in runners:
            await r.cleanup()
        return ok.status, redirected.status, direct.status

    assert asyncio.run(go()) == (200, 403, 403)
    assert hits == []  # ни перенаправление, ни картинка, ни прямой переход не дошли до «роутера»
