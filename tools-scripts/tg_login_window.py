"""Вход в Telegram Web в обычном окне на мониторе (Александр вводит сам): тот же профиль браузера Ксении
(data/browser-profile). Ждёт до 15 минут; как только появились чаты — пишет LOGGED_IN и закрывает окно."""
import asyncio
import os
import time

from playwright.async_api import async_playwright

ROOT = "/home/user/Agents/Ksenia"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", f"{ROOT}/data/playwright")
PROFILE = f"{ROOT}/data/browser-profile"


async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(PROFILE, headless=False, locale="ru-RU", no_viewport=True,
                                                         args=["--start-maximized", "--force-device-scale-factor=1.5"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded", timeout=60000)
        print("WINDOW_OPEN", flush=True)
        t_end = time.time() + 900
        while time.time() < t_end:
            await page.wait_for_timeout(2000)
            try:
                if await page.locator(".chatlist-chat").count():
                    print("LOGGED_IN", flush=True)
                    await page.wait_for_timeout(3000)
                    await ctx.close()
                    return
            except Exception:
                print("WINDOW_CLOSED", flush=True)  # Александр закрыл окно сам
                return
        print("TIMEOUT", flush=True)
        await ctx.close()

asyncio.run(main())
