"""Планшет целиком в настоящем Chromium (необязательный тест): TLS-шлюз, вход по PIN, запись с поддельного микрофона,
WAV в «слух», реплика в «ядро», ответ голосом на «планшете», кнопки «Да»/«Нет», перебивание.

Запуск: KSENIA_TEST_CHROMIUM=/путь/к/chrome python -m pytest tests/test_pwa_browser.py
Нужны Playwright и openssl. Без переменной тест пропускается.
"""
import asyncio
import json
import math
import os
import shutil
import struct
import subprocess

import pytest
from aiohttp import web

import gateway

CHROME = os.environ.get("KSENIA_TEST_CHROMIUM")
pytestmark = pytest.mark.skipif(not CHROME or not shutil.which("openssl"), reason="нужен KSENIA_TEST_CHROMIUM и openssl")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fake_mic(path):
    """0,6 с тишины, 1 с «речи» (тон), 2 с тишины — Chrome крутит файл по кругу."""
    rate, frames = 48000, []
    for i in range(int(rate * 3.6)):
        t = i / rate
        v = 0.3 * math.sin(2 * math.pi * 220 * t) if 0.6 <= t < 1.6 else 0.0
        frames.append(struct.pack("<h", int(v * 32767)))
    data = b"".join(frames)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " +
                struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data)) + data)


class Backends:
    def __init__(self):
        self.say, self.stop, self.duck, self.wavs, self.ws = [], 0, [], [], None

    async def h_say(self, req):
        body = await req.json()
        self.say.append(body)
        ws = self.ws
        await ws.send_json({"type": "user", "text": body["text"]})
        await ws.send_json({"type": "state", "state": "thinking"})
        if body["text"] == "включи джаз":
            await ws.send_json({"type": "say", "text": "Пишу Диме: привет. Отправить?"})
            await ws.send_json({"type": "confirm", "label": "x", "question": "Пишу Диме: привет. Отправить?"})
        else:
            await ws.send_json({"type": "say", "text": "Готово."})
            await ws.send_json({"type": "confirm_clear", "id": "x", "reason": "taken"})
        await ws.send_json({"type": "audio_start", "turn": len(self.say), "rate": 44100})
        tone = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 44100))) for i in range(44100 // 2))
        for k in range(0, len(tone), 8191):  # нечётные куски — проверка склейки полусэмплов
            await ws.send_bytes(tone[k:k + 8191])
        await ws.send_json({"type": "audio_end", "turn": len(self.say)})
        await ws.send_json({"type": "state", "state": "idle"})
        return web.json_response({"reply": "ok"})

    async def h_simple(self, req):
        if req.path == "/stop":
            self.stop += 1
        else:
            self.duck.append((await req.json()).get("on"))
        return web.json_response({"ok": True})

    async def h_client(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws = ws
        await ws.send_json({"type": "hello", "busy": False, "confirm": None})
        async for _ in ws:
            pass
        return ws

    async def h_transcribe(self, req):
        body = await req.read()
        self.wavs.append(body)
        return web.json_response({"text": "включи джаз"})


def test_tablet_end_to_end(tmp_path):
    pw = pytest.importorskip("playwright.async_api")
    work = tmp_path / "k"
    (work / "pwa").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "pwa", "make-certs.sh"), work / "pwa")
    subprocess.run(["bash", "pwa/make-certs.sh"], cwd=work, check=True, capture_output=True)
    data = work / "data" / "pwa"
    (data / "pin").write_text("135790\n")
    mic = tmp_path / "mic.wav"
    fake_mic(str(mic))
    back = Backends()

    async def go():
        core = web.Application()
        core.add_routes([web.post("/say", back.h_say), web.post("/stop", back.h_simple), web.post("/duck", back.h_simple),
                         web.get("/client", back.h_client)])
        voice = web.Application()
        voice.add_routes([web.post("/transcribe", back.h_transcribe)])
        runners = []
        for app, port in ((core, 28130), (voice, 28120)):
            r = web.AppRunner(app)
            await r.setup()
            await web.TCPSite(r, "127.0.0.1", port).start()
            runners.append(r)
        gw = gateway.Gateway({"data_dir": str(data), "core_url": "http://127.0.0.1:28130",
                              "voice_in_url": "http://127.0.0.1:28120"})
        r = web.AppRunner(gw.app())
        await r.setup()
        await web.TCPSite(r, "127.0.0.1", 28140, ssl_context=gateway.tls_context(str(data))).start()
        runners.append(r)
        try:
            async with pw.async_playwright() as p:
                br = await p.chromium.launch(executable_path=CHROME, args=[
                    "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                    f"--use-file-for-fake-audio-capture={mic}", "--autoplay-policy=no-user-gesture-required"])
                ctx = await br.new_context(ignore_https_errors=True, permissions=["microphone"], bypass_csp=True)  # CSP запрещает eval, а ему — проверки Playwright
                page = await ctx.new_page()
                logs = []
                page.on("console", lambda m: logs.append(m.text))
                await page.goto("https://127.0.0.1:28140/")
                await page.wait_for_selector("#login:not([hidden])")
                await page.fill("#pin", "000000")
                await page.click("#login-button")
                await page.wait_for_function("document.getElementById('login-error').textContent.includes('Неверный')")
                await page.fill("#pin", "135790")
                await page.click("#login-button")
                await page.wait_for_selector("#app:not([hidden])")
                await page.wait_for_function("document.getElementById('state').textContent === 'Готова'")
                await page.click("#talk")
                await page.wait_for_function("document.getElementById('state').textContent === 'Слушаю'")
                # тон -> «речь», потом тишина -> конец реплики, WAV уходит в слух, реплика — в ядро
                await page.wait_for_selector("#confirm:not([hidden])", timeout=15000)
                question = await page.text_content("#confirm-q")
                user = await page.text_content("#last-user")
                await page.wait_for_function("document.getElementById('state').textContent === 'Готова'", timeout=10000)
                await page.click("#yes")
                await page.wait_for_selector("#confirm", state="hidden")
                await page.wait_for_function("document.getElementById('last-ksenia').textContent.includes('Готово')")
                await page.wait_for_function("document.getElementById('state').textContent === 'Готова'", timeout=10000)
                history = await page.eval_on_selector_all("#history li", "els => els.map(e => e.textContent)")
                await br.close()
                return question, user, history, logs
        finally:
            for r in runners:
                await r.cleanup()

    question, user, history, logs = asyncio.run(go())
    assert question == "Пишу Диме: привет. Отправить?" and user == "включи джаз"
    assert back.say == [{"text": "включи джаз", "output": "client"}, {"text": "да", "output": "client"}]
    w = back.wavs[0]
    assert w[:4] == b"RIFF" and struct.unpack("<I", w[24:28])[0] == 16000  # 16 кГц, как ждёт voice-in
    assert 0.8 < (len(w) - 44) / 32000 < 3.5  # тишина в конце обрезана
    assert back.duck[:2] == [True, False]  # музыка у ПК приглушалась на время записи
    assert history[0].startswith("Ксения") and "Готово" in history[0]  # новые сверху
    assert not [m for m in logs if "Error" in m], logs
