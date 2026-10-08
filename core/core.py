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
import urllib.parse

import aiohttp
from aiohttp import web

ROOT = os.path.dirname(os.path.abspath(__file__))

from tools import confirm, music, screen, vk  # noqa: E402  (инструменты — отдельные модули в core/tools)
from tools import web as webtool  # noqa: E402  (не путать с aiohttp.web)

TOOL_MODULES = [music, screen, vk, webtool]
TOOL_SCHEMAS = [sch for m in TOOL_MODULES for sch in m.SCHEMAS]
TOOL_INDEX = {sch["function"]["name"]: m for m in TOOL_MODULES for sch in m.SCHEMAS}


TOOL_TIMEOUT_S = 30


async def run_tool(name, arguments, session):
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "аргументы инструмента — не JSON"}
    if not isinstance(args, dict):
        return {"ok": False, "error": "аргументы инструмента должны быть объектом JSON"}
    mod = TOOL_INDEX.get(name)
    if not mod:
        return {"ok": False, "error": f"нет такого инструмента: {name}"}
    try:
        limit = getattr(mod, "TIMEOUTS", {}).get(name, TOOL_TIMEOUT_S)  # зрению нужно больше: монитор, снимок, 4K
        return await asyncio.wait_for(mod.call(name, args, session), timeout=limit)
    except asyncio.TimeoutError:  # str(TimeoutError()) пустая — модель получала «сбой инструмента: »
        return {"ok": False, "error": "инструмент не ответил вовремя"}
    except Exception as e:
        log.exception("инструмент %s", name)
        return {"ok": False, "error": f"сбой инструмента: {e!r}"[:300]}


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


THINK_RE = re.compile(r"<think>.*?</think>|<think>.*$|</?think>", re.S)


def strip_thinking(text: str) -> str:
    """Иногда при бюджете размышлений сервер не отделяет их, и <think>…</think> попадает в ответ."""
    return THINK_RE.sub("", text or "")


def clean_for_speech(text: str, verbatim: bool = False) -> str:
    """Убрать разметку и эмодзи; оставить только разрешённые пометки эмоций.

    verbatim — чужой текст (экран, буфер обмена): слова в [скобках] сохраняются, но пометками эмоций
    не становятся — иначе «[laughing]» в документе рассмешит голос, а «[Глава 1]» пропадёт."""
    def tag(m):
        t = m.group(1).strip().lower()
        return f"[{t}]" if t in ALLOWED_TAGS else ""
    if not verbatim:
        text = strip_thinking(text)
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
        except FileNotFoundError:
            return []
        except Exception as e:
            # битый файл не выбрасываем молча: сохраняем копию, иначе следующее сохранение затрёт историю
            bad = HISTORY_FILE + time.strftime(".bad-%Y%m%d-%H%M%S")
            try:
                os.replace(HISTORY_FILE, bad)
            except OSError:
                pass
            log.error("История повреждена (%s), копия: %s", e, bad)
            return []

    def save_history(self):
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.history[-CONFIG.get("history_keep", 200):], f, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_FILE)

    async def _resolve_confirmation(self, user_text: str) -> str:
        """Подтверждение рискованного действия решает ЯДРО, не модель: если действие ждёт и Александр
        ответил ясным согласием — ядро выполняет его само; любой другой ответ отменяет."""
        p = confirm.current()
        if not p:
            return ""
        if p.get("expired"):
            return f"; действие «{p['label']}» устарело и НЕ выполнено"
        if not is_affirmative(user_text):
            confirm.cancel()
            return f"; действие «{p['label']}» НЕ выполнено (Александр не сказал «да»)"
        item = confirm.take()
        try:
            res = await asyncio.wait_for(item["run"](), timeout=60)
        except Exception as e:
            log.exception("подтверждённое действие")
            res = {"ok": False, "error": f"сбой: {e!r}"[:200]}
        log.info("Подтверждено Александром: %s -> %s", item["label"], res)
        if res.get("ok"):
            return f"; Александр подтвердил, ядро ВЫПОЛНИЛО: {item['label']}. Коротко скажи итог"
        return f"; Александр подтвердил, но «{item['label']}» НЕ удалось: {res.get('error')}. Скажи честно"

    def _window(self):
        """Окно истории для мозга. Гибридный Nex пересчитывает всё при любом изменении начала,
        поэтому окно не скользит каждую реплику, а изредка прыгает вперёд большим шагом."""
        max_n = CONFIG.get("history_max", 120)
        max_chars = CONFIG.get("history_max_chars", 40000)  # ~10–12 тыс. токенов: пересчёт после промаха — секунды
        size = sum(len(str(m.get("content") or "")) for m in self.history[self.window_start:])
        if len(self.history) - self.window_start > max_n or size > max_chars:
            # прыжок: оставляем свежую половину по числу сообщений и по объёму
            start, acc = len(self.history), 0
            while start > self.window_start and len(self.history) - start < max_n // 2 and acc < max_chars // 2:
                start -= 1
                acc += len(str(self.history[start].get("content") or ""))
            self.window_start = start
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
        note = await self._resolve_confirmation(user_text)
        self.history.append({"role": "user", "content": f"{user_text}\n\n(служебно: {now_context()}{note})"})
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
                        if first_step and not first_sent:
                            sent, buf = split_first_sentence(buf)
                            if sent:
                                await queue.put((sent, False))  # первая фраза — сразу, чтобы заговорить как можно раньше
                                first_sent = True
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:  # общий таймаут aiohttp — TimeoutError, не ClientError
            log.error("brain недоступен: %r", e)
            failed = True
        if failed:
            buf = (buf.strip() + " " + BRAIN_FAIL_PHRASE).strip()
        # Промежуточный шаг (после первого и с вызовом инструмента) — это «рассуждения вслух»: не озвучиваем.
        narration = (not first_step) and bool(calls) and not failed
        if buf.strip() and not speaker.cancelled and not narration:
            await queue.put((buf.strip(), False))  # остаток одним куском: меньше пауз между фразами
        full = strip_thinking(full)
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
    try:
        data = await request.json()
        text = (data.get("text") or "").strip()
    except (ValueError, AttributeError):
        text = ""
    if not text:
        return web.json_response({"error": "нужен JSON {\"text\": \"...\"}"}, status=400)
    timings = {"_t0": time.time()}
    await ks.stop()
    await music.duck(True)
    try:
        reply = await turn(text, timings)
    finally:
        await music.duck(False)
    return web.json_response({"reply": reply, "timings": timings})


async def say_notice(text: str):
    """Служебная фраза голосом, мимо истории и мозга. Александр не видит экран: молчание ему ничего не объяснит."""
    sp = Speaker(ks.session)
    ks.speaker = sp  # «стоп» прерывает и её
    try:
        await sp.speak(text, {"_t0": time.time()})
    finally:
        await sp.finish()


BYE_WORDS = ("пока", "хватит", "стоп", "ксения стоп", "ксения, стоп", "до свидания", "отбой", "спокойной ночи", "всё, спасибо", "стоп разговор")


AFFIRM = {"да", "ага", "угу", "отправляй", "отправь", "отправить", "подтверждаю", "давай", "конечно", "верно",
          "ок", "окей", "можно", "отправляем", "yes"}
NEGATE = {"нет", "не", "отмена", "отмени", "стоп", "погоди", "подожди", "измени", "исправь", "только", "но", "поправь",
          "замени", "поменяй", "перепиши", "добавь", "убери", "кроме", "лучше", "сначала"}


def is_affirmative(text: str) -> bool:
    """Ясное согласие: короткая реплика, начинается со «да/отправляй/…» и без отрицаний."""
    words = _words(text).split()
    if not words or len(words) > 6 or any(w in NEGATE for w in words):
        return False
    return words[0] in AFFIRM or (len(words) > 1 and words[0] in ("ну", "так") and words[1] in AFFIRM)


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


LISTEN_FAIL = {
    "no_microphone": "[sigh] Не слышу микрофон. Наушники подключены?",
    "mic_lost": "[sigh] Микрофон пропал посреди фразы. Повтори, пожалуйста.",
    "busy": "[sigh] Я ещё дослушиваю прошлую фразу. Нажми ещё раз через пару секунд.",
}
LISTEN_DOWN = "[sigh] Я тебя не слышу: слух не отвечает. Проверь, пожалуйста, сервис."
CONV_CRASH = "[sigh] Ой, у меня что-то сломалось. Нажми ещё раз, пожалуйста."


class Conversation:
    """Живой диалог: слушать -> ответить -> снова слушать, пока Александр не замолчит или не попрощается."""

    def __init__(self):
        self.task = None
        self.turns = 0

    def active(self):
        return self.task is not None and not self.task.done()

    async def listen(self):
        """Один запрос к voice-in -> {"text", "timings"} или {"error"}. Занят прошлой записью
        (после перебивания) — ждём и пробуем снова, а не заканчиваем разговор молча."""
        deadline = time.time() + CONFIG.get("listen_busy_wait_s", 20)
        while True:
            async with ks.session.post(CONFIG["voice_in_url"] + "/listen",
                                       timeout=aiohttp.ClientTimeout(total=90)) as r:
                if r.status == 409 and time.time() < deadline:
                    await asyncio.sleep(0.3)
                    continue
                try:
                    heard = await r.json(content_type=None)
                except ValueError:
                    heard = None
                if not isinstance(heard, dict):
                    heard = {}
                if r.status != 200:
                    heard.setdefault("error", f"http {r.status}")
                return heard

    async def run(self):
        self.turns = 0
        await music.duck(True)
        try:
            while True:
                try:
                    heard = await self.listen()
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.error("voice-in недоступен: %r", e)
                    await say_notice(LISTEN_DOWN)
                    return
                info = heard.get("timings") or {}
                if heard.get("error"):
                    log.error("voice-in: %s", heard["error"])
                    await say_notice(LISTEN_FAIL.get(heard["error"], LISTEN_DOWN))
                    return
                text = str(heard.get("text") or "").strip()
                if not text or len(text) < 2:
                    if info.get("reason") == "mic_lost":
                        await say_notice(LISTEN_FAIL["mic_lost"])
                    log.info("Тишина — разговор окончен (%s)", info)
                    return
                timings = {"_t0": time.time(), "listen": info}
                await turn(text, timings)
                self.turns += 1
                if is_goodbye(text) or self.turns >= CONFIG.get("max_turns", 50):
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("Сбой разговора: %s", e)
            await say_notice(CONV_CRASH)
        finally:
            await music.duck(False)


conv = Conversation()
talk_lock = asyncio.Lock()


async def stop_conversation():
    """Прервать разговор и дождаться его уборки (ответы на вызовы инструментов, громкость музыки)."""
    old = conv.task if conv.active() else None
    if old:
        old.cancel()
    await ks.stop()
    if old:
        await asyncio.wait({old}, timeout=3)
    return old is not None


async def handle_talk(request):
    # Нажатие во время разговора: прервать речь Ксении и сразу слушать заново.
    # Замок — чтобы два быстрых нажатия не запустили два разговора сразу.
    async with talk_lock:
        if await stop_conversation():
            await asyncio.sleep(0.2)
        conv.task = asyncio.create_task(conv.run())
    return web.json_response({"ok": True, "mode": "conversation"})


async def handle_stop(request):
    async with talk_lock:
        await stop_conversation()
    return web.json_response({"ok": True})


async def handle_status(request):
    return web.json_response({"busy": ks.lock.locked(), "conversation": conv.active(), "turns": conv.turns,
                              "history": len(ks.history), "sink": await asyncio.to_thread(pick_output_sink)})


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


BACKGROUND = []


async def on_start(app):
    ks.session = aiohttp.ClientSession()
    # ссылки на фоновые задачи храним: цикл событий держит задачи только слабыми ссылками
    BACKGROUND.extend([asyncio.create_task(music.book_autosave_loop()), asyncio.create_task(warmup())])


async def on_cleanup(app):
    await stop_conversation()
    for t in BACKGROUND:
        t.cancel()
    await ks.session.close()


def main():
    app = web.Application(middlewares=[local_only])
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([web.post("/say", handle_say), web.post("/talk", handle_talk),
                    web.post("/stop", handle_stop), web.get("/status", handle_status)])
    web.run_app(app, host="127.0.0.1", port=CONFIG.get("port", 18130), print=None)


if __name__ == "__main__":
    main()
