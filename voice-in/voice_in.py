"""Слух Ксении (voice-in): микрофон -> конец реплики -> Whisper -> текст.

HTTP на 127.0.0.1:18120:
  POST /listen      — включить микрофон, дождаться реплики, вернуть {"text": ..., "timings": {...}}
  POST /transcribe  — распознать присланный WAV (для веб-приложения и проверок)
  GET  /status      — состояние

Bluetooth-гарнитура: микрофон есть только в профиле HFP (headset-head-unit, 16 кГц).
На время реплики карта переключается в HFP, затем сразу возвращается в A2DP (высокое качество).
"""
import asyncio
import io
import json
import logging
import math
import os
import subprocess
import time
import urllib.parse

import numpy as np
import soundfile as sf
from aiohttp import web
from faster_whisper import WhisperModel

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
RATE = 16000
FRAME = 320  # 20 мс

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("voice-in")


def sh(*args, timeout=5):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout


def find_bt_card():
    """Первая подключённая Bluetooth-карта с профилем гарнитуры."""
    for line in sh("pactl", "list", "cards", "short").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("bluez_card."):
            return parts[1]
    return None


def card_profile(card):
    out = sh("pactl", "list", "cards")
    cur = None
    for line in out.splitlines():
        if line.startswith("Card #"):
            cur = None
        s = line.strip()
        if s.startswith("Name: "):
            cur = s[6:]
        elif cur == card and s.startswith("Active Profile: "):
            return s[len("Active Profile: "):]
    return None


def find_source(card):
    """Источник микрофона: из конфига или bluez_input той же гарнитуры."""
    if CONFIG.get("source") and CONFIG["source"] != "auto":
        return CONFIG["source"]
    if card:
        mac = card[len("bluez_card."):].replace("_", ":")
        return f"bluez_input.{mac}"
    return None


def make_beep(freq=880, ms=110, vol=0.25):
    n = int(RATE * ms / 1000)
    t = np.arange(n) / RATE
    env = np.minimum(1.0, np.minimum(t / 0.01, (ms / 1000 - t) / 0.02))
    x = (vol * env * np.sin(2 * math.pi * freq * t) * 32767).astype(np.int16)
    return x.tobytes()


# 350 мс тишины: голосовой канал HFP (SCO) поднимается не мгновенно, иначе начало сигнала теряется
HALLUCINATIONS = ("продолжение следует", "субтитры", "спасибо за просмотр", "подписывайтесь",
                  "редактор субтитров", "корректор", "dimatorzok", "до новых встреч")

BEEP = b"\x00\x00" * int(RATE * 0.35) + make_beep(freq=880, ms=200, vol=0.3)


def clean_gigaam(t: str) -> str:
    """GigaAM e2e оформляет речь как диалог в книге: «— Фраза.— Ещё.» — убираем тире в начале реплик
    и сдвоенную пунктуацию («.!»)."""
    import re
    t = re.sub(r"(^|[.!?…])\s*[—–-]\s+", r"\1 ", t.strip())
    t = re.sub(r"[.]([!?])", r"\1", t)
    return " ".join(t.split()).strip()


class Ear:
    def __init__(self):
        t0 = time.time()
        # Движок распознавания: GigaAM v3 e2e-CTC (по замерам 2026-10-08: та же точность 4,0%, в 11 раз быстрее
        # Whisper, CTC не «выдумывает» текст на шуме) или Whisper (запасной). Переключение — config.json "engine".
        self.engine = CONFIG.get("engine", "gigaam")
        if self.engine == "gigaam":
            import onnx_asr
            self.model = onnx_asr.load_model(CONFIG.get("gigaam_model", "gigaam-v3-e2e-ctc"),
                                             providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
            self.model.recognize(np.zeros(RATE, dtype=np.float32), sample_rate=RATE)  # прогрев
        else:
            self.model = WhisperModel(CONFIG["model_dir"], device="cuda", compute_type=CONFIG.get("compute_type", "int8_float16"))
        log.info("Распознавание (%s) загружено за %.1f с", self.engine, time.time() - t0)
        self.lock = asyncio.Lock()
        self.busy = False

    @staticmethod
    def _max_rms(frames):
        return max((float(np.sqrt(np.mean((f.astype(np.float32) / 32768.0) ** 2))) for f in frames), default=0.0)

    @staticmethod
    def _save_debug(frames):
        # последняя запись микрофона — для разбора (перезаписывается каждый раз)
        try:
            sf.write(os.path.join(ROOT, "..", "logs", "last_listen.wav"), np.concatenate(frames), RATE)
        except Exception:
            pass

    def transcribe(self, pcm16: np.ndarray):
        audio = pcm16.astype(np.float32) / 32768.0
        if self.engine == "gigaam":
            return clean_gigaam(self.model.recognize(audio, sample_rate=RATE) or "")
        segs, info = self.model.transcribe(
            audio, language=CONFIG.get("language", "ru"), beam_size=CONFIG.get("beam_size", 5),
            vad_filter=False, condition_on_previous_text=False,
            initial_prompt=CONFIG.get("initial_prompt") or None)
        good = [s for s in segs if not (s.no_speech_prob > 0.6 and s.avg_logprob < -0.7)]
        text = " ".join(s.text.strip() for s in good).strip()
        low = text.lower().strip(" .!…")
        if any(h in low for h in HALLUCINATIONS) and len(low) < 60:
            log.info("Отброшена типичная галлюцинация Whisper: %s", text)
            return ""
        return text

    async def record_utterance(self, source, sink_for_beep, client_gone=lambda: False):
        """Запись до конца реплики: энергетический детектор с адаптивным порогом шума."""
        silence_ms = CONFIG.get("silence_ms", 800)
        max_s = CONFIG.get("max_s", 30)
        start_timeout = CONFIG.get("start_timeout_s", 12)
        if sink_for_beep and CONFIG.get("beep", True):
            p = await asyncio.create_subprocess_exec(
                "pacat", "--playback", "-d", sink_for_beep, "--raw", f"--rate={RATE}", "--channels=1",
                "--format=s16le", "--latency-msec=30", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            p.stdin.write(BEEP)
            await p.stdin.drain()
            p.stdin.close()
            await p.wait()
        rec = await asyncio.create_subprocess_exec(
            "parec", "-d", source, "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le",
            "--latency-msec=20", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        frames, speech_started, silent_ms, noise, voiced_win = [], False, 0, None, []
        t_start = time.time()
        t_speech = None
        try:
            while True:
                try:
                    buf = await asyncio.wait_for(rec.stdout.readexactly(FRAME * 2), timeout=3)
                except (asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                    # микрофон пропал (наушники отключились, parec упал, SCO не отдаёт звук): раньше — 500 и тишина;
                    # теперь распознаём то, что успели записать, или честно сообщаем ядру
                    log.warning("Микрофон оборвался: %r", e)
                    if speech_started:
                        break
                    self._save_debug(frames)
                    return None, {"reason": "mic_lost"}
                if client_gone():
                    # ядро разорвало соединение (Александр перебил или нажал «стоп») — микрофон сразу освобождаем
                    log.info("Запрос отменён ядром — запись прекращена")
                    return None, {"reason": "cancelled"}
                x = np.frombuffer(buf, dtype=np.int16)
                frames.append(x)
                rms = float(np.sqrt(np.mean((x.astype(np.float32) / 32768.0) ** 2)))
                elapsed = time.time() - t_start
                if elapsed < CONFIG.get("ignore_start_s", 0.35):
                    continue  # хвост сигнала ещё звучит в наушниках и попадает в микрофон
                if noise is None:
                    noise = rms
                if not speech_started:
                    noise = 0.95 * noise + 0.05 * rms if elapsed > 0.1 else noise
                    thr = max(noise * 3.0, CONFIG.get("min_speech_rms", 0.012))
                    # окно 300 мс: речь засчитывается, если в нём набралось >= min_voiced_ms голоса
                    # (с провалами — микрофон JBL глушит паузы до нуля)
                    voiced_win.append(rms > thr)
                    if len(voiced_win) > 15:
                        voiced_win.pop(0)
                    if sum(voiced_win) * 20 >= CONFIG.get("min_voiced_ms", 120):
                        speech_started, t_speech = True, time.time()
                    elif elapsed > start_timeout:
                        self._save_debug(frames)
                        return None, {"reason": "no_speech", "noise_rms": round(noise, 4),
                                      "max_rms": round(self._max_rms(frames), 4)}
                else:
                    thr = max(noise * 2.0, CONFIG.get("min_speech_rms", 0.012) * 0.7)
                    silent_ms = silent_ms + 20 if rms < thr else 0
                    if silent_ms >= silence_ms or elapsed > max_s:
                        break
        finally:
            rec.kill()
            await rec.wait()
        self._save_debug(frames)
        pcm = np.concatenate(frames)
        # отрезаем хвост тишины, оставляя 200 мс
        cut = max(0, silent_ms - 200) * RATE // 1000
        if cut:
            pcm = pcm[:-cut]
        return pcm, {"speech_start_s": round(t_speech - t_start, 2), "audio_s": round(len(pcm) / RATE, 2)}


ear: Ear = None


async def set_profile(card, profile):
    await asyncio.to_thread(sh, "pactl", "set-card-profile", card, profile)


async def restore_a2dp(card, profile):
    """Вернуть высокое качество и дождаться, пока выход наушников снова готов. Возвращает секунды."""
    t0 = time.time()
    await set_profile(card, profile)
    sink = "bluez_output." + card[len("bluez_card."):].replace("_", ":")
    for _ in range(40):
        # pactl — в отдельном потоке: синхронный вызов останавливал весь цикл событий слуха
        if await asyncio.to_thread(card_profile, card) == profile and \
                sink in await asyncio.to_thread(sh, "pactl", "list", "sinks", "short"):
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(CONFIG.get("a2dp_settle_s", 0.3))
    return round(time.time() - t0, 2)


async def handle_listen(request):
    if ear.lock.locked():
        return web.json_response({"error": "busy"}, status=409)
    async with ear.lock:
        t0 = time.time()
        timings = {}
        card = await asyncio.to_thread(find_bt_card) if CONFIG.get("bluetooth", True) else None
        source = find_source(card)
        if not source:
            return web.json_response({"error": "no_microphone"}, status=503)
        restore = None
        beep_sink = CONFIG.get("beep_sink") or None
        if card:
            prof = await asyncio.to_thread(card_profile, card)
            if prof != CONFIG.get("hfp_profile", "headset-head-unit"):
                restore = prof
                await set_profile(card, CONFIG.get("hfp_profile", "headset-head-unit"))
                await asyncio.sleep(CONFIG.get("hfp_settle_s", 0.6))
            if not beep_sink:
                beep_sink = "bluez_output." + card[len("bluez_card."):].replace("_", ":")
        timings["mic_ready_s"] = round(time.time() - t0, 2)
        restore_task = None
        try:
            pcm, info = await ear.record_utterance(
                source, beep_sink, client_gone=lambda: request.transport is None or request.transport.is_closing())
        finally:
            if restore:
                restore_task = asyncio.create_task(restore_a2dp(card, restore))
        timings.update(info)
        if pcm is None:
            if restore_task:
                await restore_task
            return web.json_response({"text": "", "timings": timings})
        t1 = time.time()
        text = await asyncio.to_thread(ear.transcribe, pcm)
        timings["stt_s"] = round(time.time() - t1, 2)
        if restore_task:
            # ответ Ксении должен играть уже в A2DP: ждём конца переключения (идёт параллельно с распознаванием)
            timings["a2dp_ready_s"] = await restore_task
        timings["total_s"] = round(time.time() - t0, 2)
        log.info("Услышала (%s): %s", timings, text)
        return web.json_response({"text": text, "timings": timings})


async def handle_transcribe(request):
    data = await request.read()
    audio, sr = sf.read(io.BytesIO(data), dtype="int16")
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype(np.int16)
    if sr != RATE:
        idx = np.round(np.arange(0, len(audio), sr / RATE)).astype(int)
        audio = audio[idx[idx < len(audio)]]
    t1 = time.time()
    async with ear.lock:
        text = await asyncio.to_thread(ear.transcribe, audio)
    return web.json_response({"text": text, "stt_s": round(time.time() - t1, 2)})


async def handle_status(request):
    card = find_bt_card()
    return web.json_response({"busy": ear.lock.locked(), "bt_card": card,
                              "profile": card_profile(card) if card else None, "source": find_source(card)})


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _hostname(value: str):
    try:
        return urllib.parse.urlsplit("//" + value.split("://", 1)[-1]).hostname
    except ValueError:
        return None


@web.middleware
async def local_only(request, handler):
    """Только программы этого компьютера. Любая веб-страница в браузере может послать POST на 127.0.0.1 (CSRF)
    или подменить свой DNS на 127.0.0.1 и читать ответы (DNS rebinding) — узнаём их по заголовкам Host и Origin."""
    origin = request.headers.get("Origin")
    if _hostname(request.headers.get("Host", "")) not in LOCAL_HOSTS or \
            (origin is not None and _hostname(origin) not in LOCAL_HOSTS):
        log.warning("Отклонён запрос %s %s: Host=%s Origin=%s", request.method, request.path,
                    request.headers.get("Host"), origin)
        return web.json_response({"error": "forbidden"}, status=403)
    return await handler(request)


def main():
    global ear
    ear = Ear()
    app = web.Application(client_max_size=50 * 1024 * 1024, middlewares=[local_only])
    app.add_routes([web.post("/listen", handle_listen), web.post("/transcribe", handle_transcribe),
                    web.get("/status", handle_status)])
    web.run_app(app, host="127.0.0.1", port=CONFIG.get("port", 18120), print=None)


if __name__ == "__main__":
    main()
