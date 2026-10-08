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
import subprocess
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("core")


def read_key(path):
    """Ключ мозга. Без него ядро всё равно стартует: лучше сказать голосом «мозг не отвечает», чем молча падать."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        log.error("Нет ключа мозга %s: %s", path, e)
        return ""


BRAIN_KEY = read_key(CONFIG["brain_key_file"])

ALLOWED_TAGS = {"laughing", "sigh", "teasing", "excited", "surprised", "whisper", "annoyed", "warm"}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def now_context():
    n = datetime.datetime.now()
    return f"Сейчас {WEEKDAYS[n.weekday()]}, {n.day} {MONTHS[n.month - 1]} {n.year} года, {n.strftime('%H:%M')}."


def clean_for_speech(text: str, verbatim: bool = False) -> str:
    """Убрать разметку и эмодзи; оставить только разрешённые пометки эмоций.

    verbatim — чужой текст (экран, буфер обмена): слова в [скобках] сохраняются, но пометками эмоций
    не становятся — иначе «[laughing]» в документе рассмешит голос, а «[Глава 1]» пропадёт."""
    def tag(m):
        t = m.group(1).strip().lower()
        return f"[{t}]" if t in ALLOWED_TAGS else ""
    if verbatim:
        text = re.sub(r"[\[\]]", " ", text)
        text = re.sub(r"https?://\S+", " ссылка ", text)
    else:
        text = re.sub(r"\[([^\]]+)\]\(https?://[^)\s]*\)", r"\1", text)  # [текст](ссылка) -> текст
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


def _cut_point(sent: str, limit: int) -> int:
    """Где резать слишком длинное предложение: по запятой во второй половине, иначе по пробелу, иначе по лимиту."""
    comma = sent.rfind(", ", 0, limit)
    if comma > limit // 2:
        return comma + 1  # запятая остаётся в первом куске
    space = sent.rfind(" ", 0, limit + 1)
    return space if space > limit // 3 else limit


def split_for_reading(text: str, max_len: int = 220):
    """Длинный текст -> куски по предложениям, каждый не длиннее max_len (одна озвучка s2 — не больше ~45 с звука)."""
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+|\n+", text):
        sent = sent.strip()
        if not sent:
            continue
        while len(sent) > max_len:  # очень длинное предложение режем по запятым/пробелам
            room = max_len - len(cur) - 1 if cur else max_len
            if cur and room < max_len // 3:
                parts.append(cur)
                cur, room = "", max_len
            cut = _cut_point(sent, room)
            piece = sent[:cut].strip()
            parts.append(f"{cur} {piece}" if cur else piece)
            cur, sent = "", sent[cut:].lstrip(" ,")
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
    try:
        out = subprocess.run(["pactl", "list", "sinks", "short"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError) as e:  # нет pactl или PipeWire завис — играем в выход по умолчанию
        log.warning("pactl: %s", e)
        return want if want != "auto" else None
    names = [s.split("\t")[1] for s in out.split("\n") if "\t" in s]
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

    FINISH_TIMEOUT_S = 15

    def __init__(self, session):
        self.session = session
        self.player = None
        self.cancelled = False
        self.recorded = bytearray()  # копия всего, что ушло в наушники (для разбора помех)

    async def _ensure_player(self):
        if self.player is None or self.player.returncode is not None:
            sink = await asyncio.to_thread(pick_output_sink)  # pactl — не в цикле событий
            args = ["pacat", "--playback", "--raw", "--rate=44100", "--channels=1", "--format=s16le",
                    "--latency-msec=60"]
            if sink:
                args += ["-d", sink]
            self.player = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)

    async def _drop_player(self):
        """Закрыть сломанный плеер: следующая фраза откроет новый (возможно, уже в другой выход)."""
        p, self.player = self.player, None
        if p and p.returncode is None:
            try:
                p.kill()
                await asyncio.wait_for(p.wait(), timeout=2)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass

    async def speak(self, text: str, timings: dict, verbatim: bool = False):
        """Озвучить одну фразу. Никогда не бросает исключений: сбой одной фразы не должен глушить остальные."""
        text = clean_for_speech(text, verbatim=verbatim)
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
        written = 0
        try:
            while not self.cancelled:
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
                        written += len(chunk)
                        self.recorded.extend(chunk)
                        await self.player.stdin.drain()
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.error("voice-out недоступен или оборвал поток: %r", e)
        except OSError as e:  # pacat закрылся (наушники отключились) — BrokenPipe/ConnectionReset при drain
            log.error("плеер закрылся: %r", e)
            await self._drop_player()
        except Exception:
            log.exception("сбой озвучки")
            await self._drop_player()
        finally:
            if written % 2 and self.player and self.player.returncode is None and not self.cancelled:
                # поток оборвался посреди сэмпла: без выравнивания все следующие фразы зазвучат треском
                self.player.stdin.write(b"\x00")
                self.recorded.append(0)

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
        try:
            self.save_recording()
        except OSError as e:
            log.warning("запись ответа не сохранена: %s", e)
        if self.player and self.player.returncode is None:
            try:
                self.player.stdin.close()
                # pacat доигрывает свой буфер (около секунды); если завис — не держим разговор вечно
                await asyncio.wait_for(self.player.wait(), timeout=self.FINISH_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("pacat не закончил за 15 с — останавливаю")
                await self._drop_player()
            except Exception:
                pass

    async def cancel(self):
        self.cancelled = True
        await self._drop_player()


ACTION_PATTERNS = [r"\bвключ", r"\bвыключ", r"\bпостав", r"\bпауз", r"\bпродолж", r"\bнайди",
                   r"\bоткрой", r"\bзакрой", r"\bсделай", r"\bследующ", r"\bпредыдущ",
                   r"что (это |сейчас |там )?играет", r"\bзапусти", r"\bостанов", r"\bнапомни", r"\bнапиши", r"\bотправь",
                   r"\bбыстрее", r"\bмедленнее", r"\bпереключи", r"\bстоп\b",
                   # без \b: прочитай/зачитай/почитай, погромче/потише
                   r"читай", r"громче", r"тише", r"\bгромкост", r"\bубав", r"\bприбав", r"\bсмени",
                   # зрение и лупа
                   r"\bэкран\w{0,2}\b", r"\bокн[оаеу]\b", r"\bопиши", r"\bпосмотри", r"\bпокажи",
                   r"\bувелич", r"\bуменьш", r"\bлуп[аеуы]\b", r"\bскопир", r"\bвыделен"]

HISTORY_FILE = os.path.join(ROOT, "..", "data", "history.json")


CANCELLED_RESULT = {"ok": False, "error": "отменено: Александр перебил, инструмент не выполнен или выполнен не до конца"}
BRAIN_FAIL_PHRASE = "[sigh] Ой, у меня что-то с головой. Мозг не отвечает, проверь, пожалуйста, сервис."


def parse_stream_line(raw: bytes):
    """Строка потока llama-server -> (delta | None, ошибка | None). Пустые, служебные и битые строки — (None, None)."""
    line = raw.decode("utf-8", "replace").strip()
    if not line.startswith("data:"):
        return None, None
    payload = line[5:].strip()
    if payload == "[DONE]":
        return None, None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        log.warning("brain: битая строка потока: %s", payload[:120])
        return None, None
    if not isinstance(obj, dict):
        return None, None
    if obj.get("error"):
        return None, f"ошибка в потоке: {str(obj['error'])[:300]}"
    choices = obj.get("choices")
    if not choices or not isinstance(choices[0], dict):
        return None, None  # например, последний кусок только с usage
    return choices[0].get("delta") or {}, None


def merge_tool_call(calls: dict, tc: dict):
    """Дописать кусок потокового tool_call в слот по index (имя и аргументы приходят частями)."""
    slot = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                 "function": {"name": "", "arguments": ""}})
    if tc.get("id"):
        slot["id"] = tc["id"]
    fn = tc.get("function") or {}
    slot["function"]["name"] += fn.get("name") or ""
    slot["function"]["arguments"] += fn.get("arguments") or ""


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
        start = self.window_start
        while start < len(self.history) and self.history[start]["role"] != "user":
            start += 1
        if start >= len(self.history):
            # прыжок проскочил последнюю реплику пользователя (длинная цепочка инструментов) — окно не должно опустеть
            users = [i for i, m in enumerate(self.history) if m["role"] == "user"]
            start = users[-1] if users else self.window_start
        self.window_start = start
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
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    part, verbatim = item
                    await speaker.speak(part, timings, verbatim=verbatim)
            finally:
                await speaker.finish()

        worker = asyncio.create_task(tts_worker())
        spoken_all = []
        pending = []  # вызовы инструментов, на которые ещё нет ответа в истории
        budget = self.budget_for(user_text)  # динамический бюджет: болтовня 0, задача — больше
        try:
            for step in range(CONFIG.get("max_steps", 6)):
                content, calls, failed = await self._step(budget, queue, speaker, timings, first_step=(step == 0))
                spoken_all.append(content)
                msg = {"role": "assistant", "content": content}
                if calls:
                    msg["tool_calls"] = calls
                self.history.append(msg)
                pending = list(calls)
                if not calls or speaker.cancelled:
                    break
                any_error = False
                for c in calls:
                    if speaker.cancelled:  # «стоп» посреди цепочки — остальные вызовы не исполняем
                        break
                    result = await run_tool(c["function"]["name"], c["function"].get("arguments") or "{}", self.session)
                    any_error = any_error or not result.get("ok", False)
                    log.info("Инструмент %s(%s) -> %s", c["function"]["name"], c["function"].get("arguments"),
                             {k: (v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v) for k, v in result.items()})
                    if result.get("speak_verbatim") and not speaker.cancelled:
                        # дословное чтение: текст идёт прямо в голос кусками по предложениям, без пересказа мозгом
                        for part in split_for_reading(result["speak_verbatim"]):
                            await queue.put((part, True))
                    self.history.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                         "content": json.dumps(result, ensure_ascii=False)})
                    pending.remove(c)
                if speaker.cancelled:
                    break
                # после инструмента — подумать чуть больше; после ошибки — ещё больше
                budget = CONFIG.get("budget_hard", 4096) if any_error else CONFIG.get("budget_task", 512)
            await queue.put(None)
            await worker
        finally:
            # Перебили или упали посреди хода. История по-прежнему только дописывается, но каждый вызов
            # инструмента обязан получить ответ — иначе следующий запрос к мозгу будет с «висящим» вызовом.
            for c in pending:
                self.history.append({"role": "tool", "tool_call_id": c.get("id", ""),
                                     "content": json.dumps(CANCELLED_RESULT, ensure_ascii=False)})
            if not worker.done():  # озвучка не должна жить дольше хода (и держать pacat)
                await speaker.cancel()
                worker.cancel()
            try:
                self.save_history()
            except OSError as e:
                log.error("история не сохранена: %s", e)
        timings["llm_done_s"] = round(time.time() - timings["_t0"], 2)
        full = " ".join(x for x in spoken_all if x).strip()
        self.last_tag = (re.match(r"\s*\[(\w+)\]", full) or [None, None])[1]
        return full

    async def _step(self, budget, queue, speaker, timings, first_step):
        """Один запрос к мозгу: речь идёт в озвучку по ходу, вызовы инструментов собираются.

        Возвращает (текст, вызовы, сбой). При сбое или перебивании вызовы отбрасываются: их аргументы
        могли оборваться на полуслове, а исполнять половину команды нельзя."""
        msgs = [{"role": "system", "content": PERSONA}] + self._window()
        # max_tokens у llama-server считает и токены рассуждений: без запаса на бюджет мысль на 512/4096 токенов
        # обрывается на 400-м, и ответа нет вовсе (тишина после ошибки инструмента)
        body = {"messages": msgs, "stream": True, "max_tokens": CONFIG.get("max_tokens", 400) + budget,
                "thinking_budget_tokens": budget, "tools": TOOL_SCHEMAS}
        full, buf, first_sent = "", "", False
        calls = {}
        failed = False
        try:
            async with self.session.post(CONFIG["brain_url"] + "/v1/chat/completions", json=body,
                                         headers={"Authorization": "Bearer " + BRAIN_KEY},
                                         timeout=aiohttp.ClientTimeout(total=180)) as r:
                if r.status != 200:
                    log.error("brain ответил %s: %s", r.status, (await r.text())[:300])
                    failed = True
                else:
                    async for raw in r.content:
                        if speaker.cancelled:
                            break
                        d, err = parse_stream_line(raw)
                        if err:
                            log.error("brain: %s", err)
                            failed = True
                            break
                        if d is None:
                            continue
                        for tc in d.get("tool_calls") or []:
                            merge_tool_call(calls, tc)
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
                                await queue.put((sent, False))  # первая фраза — сразу, чтобы заговорить как можно раньше
                                first_sent = True
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:  # общий таймаут aiohttp — TimeoutError, не ClientError
            log.error("brain недоступен: %r", e)
            failed = True
        if failed:
            buf = (buf.strip() + " " + BRAIN_FAIL_PHRASE).strip()
        if buf.strip() and not speaker.cancelled:
            await queue.put((buf.strip(), False))  # остаток одним куском: меньше пауз между фразами
        if failed or speaker.cancelled:
            return full.strip(), [], failed
        return full.strip(), [calls[i] for i in sorted(calls)], failed

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


def _words(text: str) -> str:
    """Нижний регистр, ё -> е (Whisper пишет по-разному), только слова через пробел."""
    return " ".join(re.findall(r"\w+", text.lower().replace("ё", "е")))


BYE_PHRASES = [_words(w) for w in BYE_WORDS]


def is_goodbye(text: str) -> bool:
    """Прощание — целыми словами: в конце реплики или в начале короткой («пока, Ксения»).
    «Покажи экран» и «пока я готовлю, включи музыку» — не прощание."""
    t = _words(text)
    n_words = len(t.split())
    for w in BYE_PHRASES:
        if t == w or t.endswith(" " + w):
            return True
        if t.startswith(w + " ") and n_words <= len(w.split()) + 2:
            return True
    return False


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
