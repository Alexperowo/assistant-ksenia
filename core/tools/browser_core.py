"""Общий браузер Ксении: один Chromium (Playwright) со своим профилем, без окна.

Профиль: data/browser-profile (вход во ВКонтакте выполнен там). Браузер запускается лениво при первом
обращении и живёт, пока работает ядро. Страницы открываются по назначению (vk, web) и переиспользуются.
"""
import asyncio
import os

from tools import net_guard

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(ROOT, "..", "..", "data"))
PROFILE = os.path.join(DATA, "browser-profile")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(DATA, "playwright"))

_pw = None
_ctx = None
_pages = {}
_lock = asyncio.Lock()


def _forget(*_):
    """Chromium упал или закрылся: следующий вызов запустит его заново (раньше ядро держало мёртвый контекст)."""
    global _ctx
    _ctx = None
    _pages.clear()


async def context():
    global _pw, _ctx
    async with _lock:
        if _ctx is not None:
            try:
                _ = _ctx.pages  # жив ли контекст
                return _ctx
            except Exception:
                _ctx = None
        from playwright.async_api import async_playwright
        if _pw is None:
            _pw = await async_playwright().start()
        # вся сеть браузера — через сторож: только публичные адреса, и на каждом шаге перенаправления
        port = await net_guard.start()
        _ctx = await _pw.chromium.launch_persistent_context(
            PROFILE, headless=True, locale="ru-RU", viewport={"width": 1280, "height": 900},
            proxy=net_guard.browser_proxy(port))
        _ctx.on("close", _forget)
        _pages.clear()
        return _ctx


async def page(purpose: str):
    """Страница под задачу (vk, web, ...): создаётся один раз, потом переиспользуется."""
    ctx = await context()
    p = _pages.get(purpose)
    if p is None or p.is_closed():
        p = await ctx.new_page()
        _pages[purpose] = p
    return p


async def close():
    global _ctx, _pw
    try:
        if _ctx:
            await _ctx.close()
        if _pw:
            await _pw.stop()
    finally:
        _ctx, _pw = None, None
        _pages.clear()
