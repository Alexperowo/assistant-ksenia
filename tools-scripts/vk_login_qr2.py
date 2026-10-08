"""Вход ВК по QR v2: держит браузер до 10 минут, шлёт QR на планшет, снимки состояния страницы;
если появилось поле кода — ждёт цифры в файле CODE_FILE и вводит их."""
import asyncio, hashlib, os, subprocess, time
from playwright.async_api import async_playwright

PROFILE = "/home/user/Agents/Ksenia/data/browser-profile"
OUT = "/home/user/Agents/Ksenia/logs/vk_qr.png"
STATE = "/home/user/Agents/Ksenia/logs/vk_state.png"
CODE_FILE = "/home/user/Agents/Ksenia/logs/vk_code.txt"
DEVICE = "889f95a316ca4c03a4a40b7627e6fbff"

async def logged_in(ctx):
    return any(c["name"].startswith("remixsid") and c.get("value") for c in await ctx.cookies(["https://vk.ru", "https://vk.com"]))

async def main():
    if os.path.exists(CODE_FILE):
        os.remove(CODE_FILE)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(PROFILE, headless=True, locale="ru-RU",
                                                         viewport={"width": 900, "height": 900}, device_scale_factor=2)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://vk.ru/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        last, t_end, code_done = None, time.time() + 600, False
        while time.time() < t_end:
            if await logged_in(ctx):
                print("LOGGED_IN", flush=True)
                await page.goto("https://vk.ru/im", wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                await page.screenshot(path=STATE)
                await ctx.close()
                return
            txt = (await page.evaluate("document.body.innerText"))[:400].replace("\n", " | ")
            inputs = page.locator("input:visible")
            n_inputs = await inputs.count()
            if n_inputs and not code_done and "наведите камеру" not in txt.lower() and ("код" in txt.lower() or "цифр" in txt.lower()):
                print("CODE_PROMPT", txt[:200], flush=True)
                await page.screenshot(path=STATE)
                while time.time() < t_end and not os.path.exists(CODE_FILE):
                    await page.wait_for_timeout(500)
                if os.path.exists(CODE_FILE):
                    code = "".join(ch for ch in open(CODE_FILE).read() if ch.isdigit())
                    await inputs.first.click()
                    await page.keyboard.type(code, delay=80)
                    await page.keyboard.press("Enter")
                    code_done = True
                    print("CODE_ENTERED", len(code), flush=True)
                    await page.wait_for_timeout(4000)
                    await page.screenshot(path=STATE)
                    continue
            shot = await page.screenshot()
            h = hashlib.md5(shot).hexdigest()
            if h != last and not code_done and "наведите камеру" in txt.lower():
                open(OUT, "wb").write(shot)
                subprocess.run(["kdeconnect-cli", "-d", DEVICE, "--share", OUT], capture_output=True)
                print("QR_SENT", time.strftime("%H:%M:%S"), "|", txt[:120], flush=True)
                last = h
            await page.wait_for_timeout(3000)
        print("TIMEOUT", flush=True)
        await ctx.close()

asyncio.run(main())
