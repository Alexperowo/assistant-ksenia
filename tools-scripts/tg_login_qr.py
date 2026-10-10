"""Вход в Telegram Web (K) по QR: держит браузер до 10 минут, шлёт QR на планшет, ждёт сканирования с телефона
(Telegram → Настройки → Устройства → Подключить устройство). Если включена облачная проверка (пароль) — ждёт
пароль в файле PASS_FILE. Профиль — общий с ядром (data/browser-profile): вход делается, пока браузер ядра не запущен."""
import asyncio, hashlib, os, subprocess, time
from playwright.async_api import async_playwright

ROOT = "/home/user/Agents/Ksenia"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", f"{ROOT}/data/playwright")
PROFILE = f"{ROOT}/data/browser-profile"
OUT, STATE, PASS_FILE = f"{ROOT}/logs/tg_qr.png", f"{ROOT}/logs/tg_state.png", f"{ROOT}/logs/tg_pass.txt"
DEVICE = "889f95a316ca4c03a4a40b7627e6fbff"


async def main():
    if os.path.exists(PASS_FILE):
        os.remove(PASS_FILE)
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(PROFILE, headless=True, locale="ru-RU",
                                                         viewport={"width": 900, "height": 900}, device_scale_factor=2)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded", timeout=60000)
        last, t_end, pass_done = None, time.time() + 600, False
        while time.time() < t_end:
            await page.wait_for_timeout(2500)
            # вошёл — только когда есть настоящие чаты и страницы входа нет (раньше срабатывало на экране входа)
            if await page.locator(".chatlist-chat").count() and not await page.locator(".auth-pages:visible").count():
                print("LOGGED_IN", flush=True)
                await page.screenshot(path=STATE)
                await ctx.close()
                return
            if await page.locator("input[type=password]").count():
                # облачный пароль: можно несколько попыток (неверный — поле снова пустое, ждём новый файл)
                if pass_done:
                    print("PASSWORD_WRONG", flush=True)
                    pass_done = False
                print("PASSWORD_PROMPT", flush=True)
                while time.time() < t_end and not os.path.exists(PASS_FILE):
                    await page.wait_for_timeout(500)
                if os.path.exists(PASS_FILE):
                    pw = open(PASS_FILE).read().strip()
                    os.remove(PASS_FILE)
                    await page.locator("input[type=password]").first.fill(pw)
                    await page.keyboard.press("Enter")
                    pass_done = True
                    print("PASSWORD_ENTERED", flush=True)
                    await page.wait_for_timeout(5000)
                continue
            qr = page.locator("canvas").first
            if await qr.count():
                shot = await qr.screenshot()
                h = hashlib.md5(shot).hexdigest()
                if h != last:  # код обновляется каждые ~30 с — слать свежий
                    open(OUT, "wb").write(shot)
                    subprocess.run(["kdeconnect-cli", "-d", DEVICE, "--share", OUT], capture_output=True)
                    print("QR_SENT", time.strftime("%H:%M:%S"), flush=True)
                    last = h
        print("TIMEOUT", flush=True)
        await ctx.close()

asyncio.run(main())
