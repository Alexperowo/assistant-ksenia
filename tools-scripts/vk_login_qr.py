"""Вход во ВКонтакте в браузере Ксении по QR-коду: снимок QR уходит на планшет (KDE Connect),
Александр сканирует его телефоном. Ждём до 6 минут, при смене кода отправляем новый."""
import asyncio, hashlib, os, subprocess, time
from playwright.async_api import async_playwright

PROFILE = "/home/user/Agents/Ksenia/data/browser-profile"
OUT = "/home/user/Agents/Ksenia/logs/vk_qr.png"
DEVICE = "889f95a316ca4c03a4a40b7627e6fbff"

async def logged_in(ctx):
    return any(c["name"].startswith("remixsid") and c.get("value") for c in await ctx.cookies(["https://vk.ru", "https://vk.com"]))

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(PROFILE, headless=True, locale="ru-RU",
                                                         viewport={"width": 900, "height": 900}, device_scale_factor=2)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://vk.ru/", wait_until="domcontentloaded", timeout=30000)
        last, t_end = None, time.time() + 360
        while time.time() < t_end:
            if await logged_in(ctx):
                print("LOGGED_IN", flush=True)
                await page.goto("https://vk.ru/im", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                await ctx.close()
                return
            await page.wait_for_timeout(1500)
            # QR-блок: ищем картинку/canvas/svg рядом с текстом «Наведите камеру»
            qr = page.locator("canvas, svg").filter(has_not=page.locator("text=ВКонтакте")).first
            try:
                box = await qr.bounding_box()
            except Exception:
                box = None
            shot = await page.screenshot(clip={"x": box["x"] - 30, "y": box["y"] - 30, "width": box["width"] + 60,
                                               "height": box["height"] + 60} if box and box["width"] > 100 else None)
            h = hashlib.md5(shot).hexdigest()
            if h != last:
                open(OUT, "wb").write(shot)
                subprocess.run(["kdeconnect-cli", "-d", DEVICE, "--share", OUT], capture_output=True)
                print("QR_SENT", time.strftime("%H:%M:%S"), "box", box and int(box["width"]), flush=True)
                last = h
                await page.wait_for_timeout(5000)
        print("TIMEOUT", flush=True)
        await ctx.close()

asyncio.run(main())
