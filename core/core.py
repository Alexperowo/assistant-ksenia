"""Ядро Ксении (core): разговор, характер, бюджет рассуждений, озвучка по фразам.

HTTP на 127.0.0.1:18130:
  POST /talk   — послушать Александра (через voice-in) и ответить голосом
  POST /say    — {"text": "..."}: ответить на набранный текст (для проверок)
  POST /stop   — замолчать (прервать текущий ответ)
  GET  /status — состояние

Этап 1: только разговор, без инструментов. Бюджет рассуждений для болтовни = 0
(Nex не слушается enable_thinking=false, но слушается thinking_budget_tokens=0).
"""
import asyncio
import datetime
import json
import logging
import os
import re
import time
import uuid

import aiohttp
from aiohttp import web

ROOT = os.path.dirname(os.path.abspath(__file__))

from tools import music, screen  # noqa: E402  (инструменты — отдельные модули в core/tools)

TOOL_MODULES = [music, screen]
TOOL_SCHEMAS = [sch for m in TOOL_MODULES for sch in m.SCHEMAS]
TOOL_INDEX = {sch["function"]["name"]: m for m in TOOL_MODULES for sch in m.SCHEMAS}


async def run_tool(name, arguments, session):
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "аргументы инструмента — не JSON"}
    mod = TOOL_INDEX.get(name)
    if not mod:
        return {"ok": False, "error": f"нет такого инструмента: {name}"}
    try:
        return await asyncio.wait_for(mod.call(name, args, session), timeout=30)
    except Exception as e:
        return {"ok": False, "error": f"сбой инструмента: {e}"}
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
PERSONA = open(os.path.join(ROOT, "prompts", "persona.md"), encoding="utf-8").read()
BRAIN_KEY = open(CONFIG["brain_key_file"]).read().strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("core")

ALLOWED_TAGS = {"laughing", "sigh", "teasing", "excited", "surprised", "whisper", "annoyed", "warm"}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def now_context():
    n = datetime.datetime.now()
    return f"Сейчас {WEEKDAYS[n.weekday()]}, {n.day} {MONTHS[n.month - 1]} {n.year} года, {n.strftime('%H:%M')}."


def clean_for_speech(text: str) -> str:
    """Убрать разметку и эмодзи; оставить только разрешённые пометки эмоций."""
    def tag(m):
        t = m.group(1).strip().lower()
        return f"[{t}]" if t in ALLOWED_TAGS else ""
    text = re.sub(r"\[([^\]]{1,30})\]", tag, text)
    text = re.sub(r"[*_#`>~|]+", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def split_first_sentence(buf: str, min_len: int = 12):
    """Вернуть (первая_фраза, остаток) если в буфере есть законченная фраза."""
    for m in re.finditer(r"[.!?…]+[\"»)]?(\s|$)|\n", buf):
        end = m.end()
        if len(buf[:end].strip()) >= min_len and (m.group(0).strip() == "" or end < len(buf)):
            return buf[:end].strip(), buf[end:]
    return None, buf


def split_for_reading(text: str, max_len: int = 220):
    """Длинный текст -> куски по предложениям (одна озвучка s2 — не больше ~45 с звука)."""
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+|\n+", text):
        sent = sent.strip()
        if not sent:
            continue
        while len(sent) > max_len:  # очень длинное предложение режем по запятым/пробелам
            cut = max(sent.rfind(", ", 0, max_len), sent.rfind(" ", 0, max_len))
            cut = cut if cut > max_len // 3 else max_len
            parts.append((cur + " " + sent[:cut]).strip()) if cur else parts.append(sent[:cut].strip())
            cur, sent = "", sent[cut:].lstrip(", ")
        if len(cur) + len(sent) + 1 > max_len and cur:
            parts.append(cur)
            cur = sent
        else:
            cur = (cur + " " + sent).strip()
    if cur:
        parts.append(cur)
    return parts


def pick_output_sink():
    """Куда говорить: настройка, иначе A2DP-выход Bluetooth-наушников, иначе HDMI, иначе по умолчанию."""
    want = CONFIG.get("output_sink", "auto")
    import subprocess
    sinks = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True).stdout.split("\n")
    names = [s.split("\t")[1] for s in sinks if "\t" in s]
    if want != "auto" and want in names:
        return want
    for n in names:
        if n.startswith("bluez_output."):
            return n
    for n in names:
        if "hdmi" in n:
            return n
    return None


class Speaker:
    """Озвучка ответа: фразы по очереди -> voice-out (поток PCM) -> pacat в выбранный выход."""

    def __init__(self, session):
        self.session = session
        self.player = None
        self.cancelled = False
        self.recorded = bytearray()  # копия всего, что ушло в наушники (для разбора помех)

    async def _ensure_player(self):
        if self.player is None or self.player.returncode is not None:
            sink = pick_output_sink()
            args = ["pacat", "--playback", "--raw", "--rate=44100", "--channels=1", "--format=s16le",
                    "--latency-msec=60"]
            if sink:
                args += ["-d", sink]
            self.player = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)

    async def speak(self, text: str, timings: dict):
        text = clean_for_speech(text)
        if not text or self.cancelled:
            return
        params = {"stream": True, "chunked": True, "stream_start_buffer_ms": 0,
                  "output_format": "pcm_s16le", "stream_holdback_frames": 0,
                  "stream_decode_stride_frames": CONFIG.get("tts_stride", 8)}
        def make_form():
            f = aiohttp.FormData(default_to_multipart=True)  # s2.cpp принимает только multipart
            f.add_field("text", text)
            f.add_field("voice", CONFIG.get("voice", "masha"))
            f.add_field("params", json.dumps(params))
            return f
        form = make_form()
        deadline = time.time() + 15
        while not self.cancelled:
            try:
                async with self.session.post(CONFIG["voice_out_url"] + "/generate", data=form,
                                             timeout=aiohttp.ClientTimeout(total=120)) as r:
                    if r.status == 503 and time.time() < deadline:
                        await asyncio.sleep(0.15)
                        form = make_form()
                        continue
                    if r.status != 200:
                        log.error("voice-out %s: %s", r.status, (await r.text())[:200])
                        return
                    await self._ensure_player()
                    async for chunk in r.content.iter_chunked(8192):
                        if self.cancelled:
                            return
                        if "first_audio_s" not in timings:
                            timings["first_audio_s"] = round(time.time() - timings["_t0"], 2)
                        self.player.stdin.write(chunk)
                        self.recorded.extend(chunk)
                        await self.player.stdin.drain()
                    return
            except aiohttp.ClientError as e:
                log.error("voice-out недоступен: %s", e)
                return

    def save_recording(self):
        if not CONFIG.get("record_replies", True) or not self.recorded:
            return
        import wave
        d = os.path.join(ROOT, "..", "logs", "replies")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + ".wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(bytes(self.recorded))
        files = sorted(os.listdir(d))
        for old in files[:-30]:
            os.remove(os.path.join(d, old))

    async def finish(self):
        self.save_recording()
        if self.player and self.player.returncode is None:
            try:
                self.player.stdin.close()
                await self.player.wait()
            except Exception:
                pass

    async def cancel(self):
        self.cancelled = True
        if self.player and self.player.returncode is None:
            self.player.kill()
            await self.player.wait()


ACTION_PATTERNS = [r"\bвключ", r"\bвыключ", r"\bпостав", r"\bпауз", r"\bпродолж", r"\bчитай", r"\bнайди",
                   r"\bоткрой", r"\bзакрой", r"\bсделай", r"\bгромче", r"\bтише", r"\bследующ", r"\bпредыдущ",
                   r"что (сейчас )?играет", r"\bзапусти", r"\bостанов", r"\bнапомни", r"\bнапиши", r"\bотправь",
                   r"\bбыстрее", r"\bмедленнее", r"\bпереключи", r"\bстоп\b"]

HISTORY_FILE = os.path.join(ROOT, "..", "data", "history.json")


class Ksenia:
    def __init__(self):
        self.history = self._load_history()
        self.window_start = 0
        self.lock = asyncio.Lock()
        self.speaker = None
        self.session = None
        self.last_tag = None

    @staticmethod
    def _load_history():
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)[-CONFIG.get("history_keep", 200):]
        except Exception:
            return []

    def save_history(self):
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.history[-CONFIG.get("history_keep", 200):], f, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_FILE)

    def _window(self):
        """Окно истории для мозга. Гибридный Nex пересчитывает всё при любом изменении начала,
        поэтому окно не скользит каждую реплику, а изредка прыгает вперёд большим шагом."""
        max_n = CONFIG.get("history_max", 120)
        if len(self.history) - self.window_start > max_n:
            self.window_start = len(self.history) - max_n // 2
        # начало окна — на реплике пользователя (нельзя начинать с ответа инструмента)
        while self.window_start < len(self.history) and self.history[self.window_start]["role"] != "user":
            self.window_start += 1
        return self.history[self.window_start:]

    def budget_for(self, text: str) -> int:
        """Динамический бюджет: болтовня — 0; реплика похожа на просьбу что-то сделать — немного подумать,
        чтобы модель не отвечала по памяти, а вызвала инструмент."""
        t = text.lower()
        if any(re.search(p, t) for p in ACTION_PATTERNS):
            return CONFIG.get("budget_action", 256)
        return CONFIG.get("budget_chat", 0)

    async def respond(self, user_text: str, timings: dict):
        # Nex — гибридная модель: её рекуррентное состояние нельзя откатить, поэтому запрос обязан
        # в точности продолжать прошлый. Время пишем в реплику и сохраняем её в истории как есть.
        self.history.append({"role": "user", "content": f"{user_text}\n\n(служебно: {now_context()})"})
        speaker = Speaker(self.session)
        self.speaker = speaker
        queue: asyncio.Queue = asyncio.Queue()

        async def tts_worker():
            while True:
                part = await queue.get()
                if part is None:
                    break
                await speaker.speak(part, timings)
            await speaker.finish()

        worker = asyncio.create_task(tts_worker())
        spoken_all = []
        budget = self.budget_for(user_text)  # динамический бюджет: болтовня 0, задача — больше
        for step in range(CONFIG.get("max_steps", 6)):
            content, calls, failed = await self._step(budget, queue, speaker, timings, first_step=(step == 0))
            spoken_all.append(content)
            msg = {"role": "assistant", "content": content}
            if calls:
                msg["tool_calls"] = calls
            self.history.append(msg)
            if not calls or speaker.cancelled:
                break
            any_error = False
            for c in calls:
                result = await run_tool(c["function"]["name"], c["function"].get("arguments") or "{}", self.session)
                any_error = any_error or not result.get("ok", False)
                log.info("Инструмент %s(%s) -> %s", c["function"]["name"], c["function"].get("arguments"),
                         {k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in result.items()})
                if result.get("speak_verbatim"):
                    # дословное чтение: текст идёт прямо в голос кусками по предложениям, без пересказа мозгом
                    for part in split_for_reading(result["speak_verbatim"]):
                        await queue.put(part)
                self.history.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                     "content": json.dumps(result, ensure_ascii=False)})
            # после инструмента — подумать чуть больше; после ошибки — ещё больше
            budget = CONFIG.get("budget_hard", 4096) if any_error else CONFIG.get("budget_task", 512)
        await queue.put(None)
        await worker
        timings["llm_done_s"] = round(time.time() - timings["_t0"], 2)
        self.save_history()
        full = " ".join(x for x in spoken_all if x).strip()
        self.last_tag = (re.match(r"\s*\[(\w+)\]", full) or [None, None])[1]
        return full

    async def _step(self, budget, queue, speaker, timings, first_step):
        """Один запрос к мозгу: речь идёт в озвучку по ходу, вызовы инструментов собираются."""
        msgs = [{"role": "system", "content": PERSONA}] + self._window()
        body = {"messages": msgs, "stream": True, "max_tokens": CONFIG.get("max_tokens", 400),
                "thinking_budget_tokens": budget, "tools": TOOL_SCHEMAS}
        full, buf, first_sent = "", "", False
        calls = {}
        try:
            async with self.session.post(CONFIG["brain_url"] + "/v1/chat/completions", json=body,
                                         headers={"Authorization": "Bearer " + BRAIN_KEY},
                                         timeout=aiohttp.ClientTimeout(total=180)) as r:
                async for raw in r.content:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:") or line.endswith("[DONE]"):
                        continue
                    d = json.loads(line[5:])["choices"][0]["delta"]
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                                     "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        slot["function"]["name"] += fn.get("name") or ""
                        slot["function"]["arguments"] += fn.get("arguments") or ""
                    delta = d.get("content") or ""
                    if not delta:
                        continue
                    if "first_token_s" not in timings:
                        timings["first_token_s"] = round(time.time() - timings["_t0"], 2)
                    full += delta
                    buf += delta
                    if first_step and not first_sent and self.last_tag and \
                            buf.lstrip().startswith(f"[{self.last_tag}]") and len(buf.strip()) > len(self.last_tag) + 2:
                        buf = buf.lstrip()[len(self.last_tag) + 2:]  # та же эмоция, что в прошлый раз, — не повторяем
                    if not first_sent:
                        sent, buf = split_first_sentence(buf)
                        if sent:
                            await queue.put(sent)  # первая фраза — сразу, чтобы заговорить как можно раньше
                            first_sent = True
                    if speaker.cancelled:
                        break
        except aiohttp.ClientError as e:
            log.error("brain недоступен: %s", e)
            buf = "[sigh] Ой, у меня что-то с головой. Мозг не отвечает, проверь, пожалуйста, сервис."
        if buf.strip():
            await queue.put(buf.strip())  # остаток одним куском: меньше пауз между фразами
        return full.strip(), [calls[i] for i in sorted(calls)], False

    async def stop(self):
        if self.speaker:
            await self.speaker.cancel()


ks = Ksenia()


async def turn(text: str, timings: dict):
    async with ks.lock:
        reply = await ks.respond(text, timings)
    timings.pop("_t0", None)
    log.info("Александр: %s | Ксения: %s | %s", text, reply, timings)
    return reply


async def handle_say(request):
    data = await request.json()
    timings = {"_t0": time.time()}
    await ks.stop()
    await music.duck(True)
    try:
        reply = await turn(data["text"], timings)
    finally:
        await music.duck(False)
    return web.json_response({"reply": reply, "timings": timings})


BYE_WORDS = ("пока", "хватит", "стоп", "ксения стоп", "ксения, стоп", "до свидания", "отбой", "спокойной ночи", "всё, спасибо", "стоп разговор")


def is_goodbye(text: str) -> bool:
    t = text.lower().strip(" .!?,")
    return any(t == w or t.startswith(w) or t.endswith(w) for w in BYE_WORDS)


class Conversation:
    """Живой диалог: слушать -> ответить -> снова слушать, пока Александр не замолчит или не попрощается."""

    def __init__(self):
        self.task = None
        self.turns = 0

    def active(self):
        return self.task is not None and not self.task.done()

    async def run(self):
        self.turns = 0
        await music.duck(True)
        try:
            while True:
                async with ks.session.post(CONFIG["voice_in_url"] + "/listen",
                                           timeout=aiohttp.ClientTimeout(total=90)) as r:
                    heard = await r.json()
                text = (heard.get("text") or "").strip()
                if not text or len(text) < 2:
                    log.info("Тишина — разговор окончен (%s)", heard.get("timings"))
                    return
                timings = {"_t0": time.time(), "listen": heard.get("timings")}
                await turn(text, timings)
                self.turns += 1
                if is_goodbye(text) or self.turns >= CONFIG.get("max_turns", 50):
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("Сбой разговора: %s", e)
        finally:
            await music.duck(False)


conv = Conversation()


async def handle_talk(request):
    # Нажатие во время разговора: прервать речь Ксении и сразу слушать заново
    if conv.active():
        conv.task.cancel()
        await ks.stop()
        await asyncio.sleep(0.2)
    else:
        await ks.stop()
    conv.task = asyncio.create_task(conv.run())
    return web.json_response({"ok": True, "mode": "conversation"})


async def handle_stop(request):
    if conv.active():
        conv.task.cancel()
    await ks.stop()
    return web.json_response({"ok": True})


async def handle_status(request):
    return web.json_response({"busy": ks.lock.locked(), "conversation": conv.active(), "turns": conv.turns,
                              "history": len(ks.history), "sink": pick_output_sink()})


async def warmup():
    """Прогреть кэш мозга текущей историей, чтобы первая реплика после перезапуска не ждала пересчёта."""
    try:
        msgs = [{"role": "system", "content": PERSONA}] + ks._window()
        if msgs[-1]["role"] == "assistant":
            body = {"messages": msgs + [{"role": "user", "content": "."}], "max_tokens": 1,
                    "thinking_budget_tokens": 0, "tools": TOOL_SCHEMAS}
            t0 = time.time()
            async with ks.session.post(CONFIG["brain_url"] + "/v1/chat/completions", json=body,
                                       headers={"Authorization": "Bearer " + BRAIN_KEY},
                                       timeout=aiohttp.ClientTimeout(total=300)) as r:
                await r.read()
            log.info("Кэш мозга прогрет за %.1f с (%d сообщений)", time.time() - t0, len(msgs))
    except Exception as e:
        log.warning("Прогрев не удался: %s", e)


async def on_start(app):
    ks.session = aiohttp.ClientSession()
    asyncio.create_task(music.book_autosave_loop())
    asyncio.create_task(warmup())


async def on_cleanup(app):
    await ks.session.close()


def main():
    app = web.Application()
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([web.post("/say", handle_say), web.post("/talk", handle_talk),
                    web.post("/stop", handle_stop), web.get("/status", handle_status)])
    web.run_app(app, host="127.0.0.1", port=CONFIG.get("port", 18130), print=None)


if __name__ == "__main__":
    main()
