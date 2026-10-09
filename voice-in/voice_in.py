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
import re
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


def bt_node(card, kind):
    """Настоящее имя выхода/микрофона гарнитуры: в классическом Bluetooth — «bluez_input.88:92:…»,
    в LE Audio — «bluez_input.88_92_….0». kind: "sinks" | "sources"."""
    mac = card[len("bluez_card."):]
    prefix = "bluez_output." if kind == "sinks" else "bluez_input."
    for line in sh("pactl", "list", kind, "short").splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and parts[1].startswith(prefix) and not parts[1].endswith(".monitor") \
                and mac.replace("_", "") in parts[1].replace("_", "").replace(":", ""):
            return parts[1]
    return None


def card_profiles(card):
    """Имена профилей карты (чтобы понять, есть ли LE Audio «звук + микрофон сразу»)."""
    out, cur, names, inside = sh("pactl", "list", "cards"), None, [], False
    for line in out.splitlines():
        st = line.strip()
        if st.startswith("Name: "):
            cur = st[6:]
        elif cur == card and st == "Profiles:":
            inside = True
        elif inside and cur == card:
            if not line.startswith("\t\t"):
                inside = False
            elif ":" in st:
                names.append(st.split(":", 1)[0])
    return names


def find_source(card):
    """Источник микрофона: из конфига или bluez_input той же гарнитуры."""
    if CONFIG.get("source") and CONFIG["source"] != "auto":
        return CONFIG["source"]
    if card:
        return bt_node(card, "sources") or "bluez_input." + card[len("bluez_card."):].replace("_", ":")
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


# Микрофон JBL глушит тихое начало слова: «Ксения» доходит как «Сеня», в LE Audio — «Седия» (живой тест 2026-10-08)
NAME_SLIPS = r"(?<!\w)(?:сеня|сенея|сения|сенья|ксеня|ксенья|ксенея|ксени|седия|седяя|кседия|ксеню)(?!\w)"


def fix_name(t: str) -> str:
    import re
    return re.sub(NAME_SLIPS, "Ксения", t, flags=re.IGNORECASE)


# Фраза, оборванная на этом слове, явно не закончена: «расскажи мне…», «включи…», «а потом и…», «э-э…»
HANGING_WORDS = set("""и а но или либо что чтобы как какой какая какое какие каким который которая где когда куда откуда
если потому поэтому то это этот эта мне меня мной тебе тебя ты я он она мы вы они его её их ему ей им
в во на с со к ко по о об обо про за из от до для у при без над под через между перед после около
ну вот так типа короче значит э ээ эээ э-э эм м мм ммм а-а слушай скажи расскажи давай включи открой найди
покажи напиши посмотри поставь сделай прочитай прочти отправь узнай запусти не очень самый ещё еще уже
заметка замечание запомни взять например примеру""".split())


def hanging(text: str) -> bool:
    """Распознанное в паузе обрывается так, что продолжение почти наверняка будет."""
    t = text.strip().lower()
    if not t:
        return False
    if t[-1] in ",-—:;" or t.endswith("...") or t.endswith("…"):
        return True
    words = re.findall(r"[\w-]+", t)
    return bool(words) and words[-1].strip("-") in HANGING_WORDS


SHORT_ANSWER = re.compile(r"^(?:да|нет|ага|угу|конечно|давай|не надо|не хочу|хочу|можно|ладно|хорошо|ок|окей|"
                          r"не знаю|наверное|согласен|верно|точно|оба|первый|второй|третий|любой)(?:[ ,]+\w+){0,2}[.!?]?$",
                          re.I)


def turn_policy(p: float, text: str, ctx: dict, cfg: dict, fast: bool = False):
    """Конец реплики: ("end", None) — договорил; ("wait", мс тишины, после которых всё же конец).

    Микрофон JBL глушит паузы до цифрового нуля, и Smart Turn почти всегда уверен «договорил» (0,97–0,99) —
    поэтому решает в первую очередь распознанный текст:
    - оборвалась на «и», «мне», запятой, «э-э» — ждать дольше (продолжение почти наверняка);
    - вопрос или восклицание, короткий ответ на вопрос Ксении — конец сразу, даже на быстрой проверке (~0,4 с);
    - иначе на обычной проверке (~0,8 с) — интонация Smart Turn, затем текст.
    Если он всё же продолжит, пока Ксения думает, ядро склеит обе части (LiveConversation)."""
    t = (text or "").strip()
    if t and hanging(t):
        return "wait", cfg.get("turn_hang_wait_ms", 3000)
    words = len(re.findall(r"\w+", t))
    strong = bool(t) and (t.endswith(("?", "!")) or (ctx.get("asked") and bool(SHORT_ANSWER.match(t))))
    if strong:
        return "end", None
    if fast:
        return "wait", None
    if p >= cfg.get("turn_threshold", 0.5) and (words or not t):
        return "end", None
    return "wait", cfg.get("turn_wait_ms", 2000)


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
            self.model_ts = self.model.with_timestamps()  # та же модель, плюс уверенность по слогам
        else:
            self.model = WhisperModel(CONFIG["model_dir"], device="cuda", compute_type=CONFIG.get("compute_type", "int8_float16"))
        log.info("Распознавание (%s) загружено за %.1f с", self.engine, time.time() - t0)
        try:
            from voiceprint import Voiceprint
            self.vp = Voiceprint(CONFIG.get("owner_threshold", 0.50), CONFIG.get("confirm_threshold", 0.55))
            log.info("Отпечаток голоса: %s", "загружен" if self.vp.centroid is not None else "образца ещё нет")
        except Exception as e:
            self.vp = None
            log.warning("Отпечаток голоса недоступен: %r", e)
        import turn
        self.turn = turn.load(log) if CONFIG.get("smart_turn", True) else None
        log.info("Конец реплики: %s", "Smart Turn v3.2" if self.turn else f"тишина {CONFIG.get('silence_ms', 800)} мс")
        self.lock = asyncio.Lock()
        self.busy = False
        self.streaming = False  # живой режим держит микрофон

    @staticmethod
    def _max_rms(frames):
        return max((float(np.sqrt(np.mean((f.astype(np.float32) / 32768.0) ** 2))) for f in frames), default=0.0)

    @staticmethod
    def _save_debug(frames):
        # записи микрофона — для разбора: последняя и 30 последних в logs/listens (только на этом компьютере)
        try:
            pcm = np.concatenate(frames)
            sf.write(os.path.join(ROOT, "..", "logs", "last_listen.wav"), pcm, RATE)
            d = os.path.join(ROOT, "..", "logs", "listens")
            os.makedirs(d, exist_ok=True)
            sf.write(os.path.join(d, time.strftime("%Y%m%d-%H%M%S") + ".wav"), pcm, RATE)
            for old in sorted(os.listdir(d))[:-30]:
                os.remove(os.path.join(d, old))
        except Exception:
            pass

    def turn_check(self, pcm16: np.ndarray):
        """(вероятность «договорил» по интонации, распознанный пока текст). Без модели — (1.0, «»)."""
        turn = getattr(self, "turn", None)
        if turn and CONFIG.get("turn_dither", True):
            import turn as turn_mod
            p = turn.complete(turn_mod.dither_zeros(pcm16))  # нули микрофона JBL -> тихий шум (см. turn.dither_zeros)
        else:
            p = turn.complete(pcm16) if turn else 1.0
        text = ""
        if getattr(self, "model", None) is not None:
            try:
                text = self.transcribe(pcm16)
            except Exception as e:
                log.warning("распознавание в паузе: %r", e)
        return p, text

    def transcribe_sure(self, pcm16: np.ndarray):
        """(текст, уверенность) — уверенность GigaAM по слогам: {"mean": средний logprob, "weak": [слова]}.
        Явный мусор («Замеер») — mean около −0,4, нормальная речь — около 0 (замер 2026-10-09)."""
        if self.engine != "gigaam" or getattr(self, "model_ts", None) is None:
            return self.transcribe(pcm16), None
        r = self.model_ts.recognize(pcm16.astype(np.float32) / 32768.0, sample_rate=RATE)
        text = fix_name(clean_gigaam(r.text or ""))
        words, cur = [], None
        for tok, lp in zip(r.tokens or [], r.logprobs or []):
            if tok.startswith(" ") or cur is None:
                cur = [tok.strip(), lp]
                words.append(cur)
            else:
                cur[0] += tok
                cur[1] += lp
        lps = [lp for tok, lp in zip(r.tokens or [], r.logprobs or []) if tok.strip() and tok.strip() not in ",.?!—-…"]
        mean = float(np.mean(lps)) if lps else 0.0
        filler = re.compile(r"^(?:[аэмх]+|хм+|ну|аа?-а+|э-э+|м-м+)$", re.I)  # «хмм», «а-а» — не важны для смысла
        weak = [w.strip(",.?!—-…") for w, lp in words if lp < CONFIG.get("asr_weak_word", -0.7)
                and w.strip(",.?!—-…") and not filler.match(w.strip(",.?!—-…"))]
        return text, {"mean": round(mean, 3), "weak": weak[:3]}

    def transcribe(self, pcm16: np.ndarray):
        audio = pcm16.astype(np.float32) / 32768.0
        if self.engine == "gigaam":
            return fix_name(clean_gigaam(self.model.recognize(audio, sample_rate=RATE) or ""))
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
        return fix_name(text)

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
        smart = CONFIG.get("smart_turn", True)
        turn_checked, turn_probs, turn_texts = False, [], []
        wait_ms = CONFIG.get("turn_wait_ms", 2000)
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
                    # первый кадр может быть хвостом сигнала (громко): шум с потолком, иначе порог
                    # взлетал выше голоса и короткое «привет» сразу после сигнала терялось (2026-10-08)
                    noise = min(rms, CONFIG.get("noise_init_max", 0.03))
                if not speech_started:
                    thr = max(noise * 3.0, CONFIG.get("min_speech_rms", 0.012))
                    voiced = rms > thr
                    if not voiced:
                        noise = 0.95 * noise + 0.05 * rms  # шум учим только по тихим кадрам, не по голосу
                    # окно 300 мс: речь засчитывается, если в нём набралось >= min_voiced_ms голоса
                    # (с провалами — микрофон JBL глушит паузы до нуля)
                    voiced_win.append(voiced)
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
                    if silent_ms == 0:
                        turn_checked, wait_ms = False, CONFIG.get("turn_wait_ms", 2000)
                    if elapsed > max_s:
                        break
                    if smart and not turn_checked and silent_ms >= CONFIG.get("turn_check_ms", 800):
                        # пауза: договорил ли? Интонация (Smart Turn) + смысл (на чём оборвалась фраза);
                        # если нет — ждём продолжения до turn_wait_ms тишины
                        turn_checked = True
                        p, partial = await asyncio.to_thread(self.turn_check, np.concatenate(frames))
                        turn_probs.append(round(p, 2))
                        if partial:
                            turn_texts.append(partial[-40:])
                        hang = hanging(partial)
                        if p >= CONFIG.get("turn_threshold", 0.5) and not hang:
                            break
                        wait_ms = CONFIG.get("turn_hang_wait_ms", 3000) if hang else CONFIG.get("turn_wait_ms", 2000)
                    if silent_ms >= (wait_ms if smart else silence_ms):
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
        return pcm, {"speech_start_s": round(t_speech - t_start, 2), "audio_s": round(len(pcm) / RATE, 2),
                     **({"turn_p": turn_probs} if turn_probs else {}),
                     **({"turn_text": turn_texts} if turn_texts else {})}


class LiveSegmenter:
    """Живой режим (LE Audio): микрофон открыт всё время, реплики режутся из непрерывного потока.

    push(кадр 20 мс) -> None | "start" (пошла речь) | "long" (речь дольше barge_ms, 1,5 с — похоже на настоящее
    перебивание, а не «круто»: тогда Ксения говорит тише; на коротких поддакиваниях громкость не меняется) | "check" (пауза: спросить turn_check и вызвать decide) | "end" (реплика готова: utterance()).
    Логика порогов — как в record_utterance; плюс 300 мс до начала речи, чтобы не терять первый слог."""

    def __init__(self, config=None):
        c = config or CONFIG
        self.c = c
        self.noise = None
        self.voiced_win, self.pre = [], []
        self.ctx = {"ksenia": "idle", "asked": False}  # что делает Ксения (присылает ядро)
        self.reset()

    def reset(self):
        self.frames, self.speaking, self.silent_ms, self.voiced_ms = [], False, 0, 0
        self.long_sent, self.checked, self.fast_checked, self.ended = False, False, False, False
        self.wait_ms = self.c.get("turn_wait_ms", 2000)
        self.probs, self.texts = [], []
        self.since_partial = 0

    def push(self, x: np.ndarray):
        rms = float(np.sqrt(np.mean((x.astype(np.float32) / 32768.0) ** 2)))
        if self.noise is None:
            self.noise = min(rms, self.c.get("noise_init_max", 0.03))
        if not self.speaking:
            thr = max(self.noise * 3.0, self.c.get("min_speech_rms", 0.012))
            voiced = rms > thr
            if not voiced:
                self.noise = 0.95 * self.noise + 0.05 * rms
            self.voiced_win = (self.voiced_win + [voiced])[-15:]
            self.pre = (self.pre + [x])[-15:]  # 300 мс до начала речи
            if sum(self.voiced_win) * 20 >= self.c.get("min_voiced_ms", 120):
                self.speaking, self.frames = True, list(self.pre)
                self.voiced_ms = sum(self.voiced_win) * 20
                self.pre, self.voiced_win = [], []
                return "start"
            return None
        self.frames.append(x)
        self.since_partial += 20
        thr = max(self.noise * 2.0, self.c.get("min_speech_rms", 0.012) * 0.7)
        if rms >= thr:
            self.silent_ms, self.checked, self.fast_checked = 0, False, False
            self.wait_ms = self.c.get("turn_wait_ms", 2000)
            self.voiced_ms += 20
            if not self.long_sent and self.voiced_ms >= self.c.get("barge_ms", 400):
                self.long_sent = True
                return "long"
            return self._maybe_partial()
        self.silent_ms += 20
        if len(self.frames) * 20 >= self.c.get("max_s", 120) * 1000:
            return "end"
        fast_ms = self.c.get("turn_fast_ms", 400)
        if fast_ms and not self.fast_checked and self.silent_ms >= fast_ms and self.silent_ms < self.c.get("turn_check_ms", 800):
            self.fast_checked = True
            return "check_fast"
        if not self.checked and self.silent_ms >= self.c.get("turn_check_ms", 800):
            self.checked = True
            return "check"
        if self.silent_ms >= self.wait_ms:
            return "end"
        return None

    def _maybe_partial(self):
        """Пока он говорит — каждые partial_ms частичное распознавание (только первые partial_max_s: для решения
        «перебивает или поддакивает» хватает начала фразы, а длинный буфер распознавать каждые 0,3 с дорого)."""
        every = self.c.get("partial_ms", 300)
        if every and self.since_partial >= every and self.voiced_ms >= self.c.get("partial_min_ms", 300) \
                and len(self.frames) * 20 <= self.c.get("partial_max_s", 6) * 1000:
            self.since_partial = 0
            return "partial"
        return None

    def pcm(self):
        return np.concatenate(self.frames) if self.frames else np.zeros(0, dtype=np.int16)

    def decide(self, p: float, partial: str, fast: bool = False):
        """Ответ turn_check на паузе: "end" — договорил, None — ждём продолжения."""
        if not fast:
            self.probs.append(round(p, 2))
        if partial:
            self.texts.append(partial[-40:])
        verdict, wait = turn_policy(p, partial, self.ctx, self.c, fast=fast)
        if verdict == "end":
            return "end"
        if wait:
            self.wait_ms = wait
        return None

    def utterance(self):
        """(pcm без хвоста тишины, сведения) и сброс к ожиданию следующей реплики."""
        pcm = self.pcm()
        cut = max(0, self.silent_ms - 200) * RATE // 1000
        if cut:
            pcm = pcm[:-cut]
        info = {"audio_s": round(len(pcm) / RATE, 2), "voiced_s": round(self.voiced_ms / 1000, 2),
                **({"turn_p": self.probs} if self.probs else {}), **({"turn_text": self.texts} if self.texts else {})}
        self.reset()
        return pcm, info


class Mood:
    """Настроение по голосу — грубо, но честно: громкость и темп реплики против обычных для Александра
    (совет Fable, REVIEW-3 п. 10.5). Обычные значения копятся отдельно для каждого микрофона (LE Audio и HFP
    звучат по-разному); подсказка — только при заметном отклонении и когда образцов уже хватает."""

    FILE = os.path.join(ROOT, "..", "data", "voice_baseline.json")

    def __init__(self):
        try:
            with open(self.FILE, encoding="utf-8") as f:
                self.base = json.load(f)
        except (OSError, ValueError):
            self.base = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.FILE), exist_ok=True)
            with open(self.FILE, "w", encoding="utf-8") as f:
                json.dump(self.base, f)
        except OSError:
            pass

    def hint(self, pcm16, text, mic="default"):
        words = len(re.findall(r"\w+", text or ""))
        if pcm16 is None or len(pcm16) < RATE or words < 3:
            return None
        f = pcm16[:len(pcm16) // FRAME * FRAME].reshape(-1, FRAME).astype(np.float32) / 32768.0
        rms = np.sqrt((f ** 2).mean(1))
        voiced = rms[rms > CONFIG.get("min_speech_rms", 0.012)]
        if len(voiced) < 15:
            return None
        loud, rate = float(np.median(voiced)), words / (len(voiced) * FRAME / RATE)
        b = self.base.setdefault(mic, {"loud": loud, "rate": rate, "n": 0})
        hint = None
        if b["n"] >= CONFIG.get("mood_min_samples", 10):
            ql, qr = loud / max(b["loud"], 1e-4), rate / max(b["rate"], 1e-3)
            if ql < 0.6 and qr < 0.85:
                hint = "говорит заметно тише и медленнее обычного — возможно, устал"
            elif ql > 1.6 and qr > 1.2:
                hint = "говорит заметно громче и быстрее обычного — возможно, взволнован или раздражён"
        a = 0.1  # обычное — медленно скользящее среднее
        b["loud"], b["rate"], b["n"] = (1 - a) * b["loud"] + a * loud, (1 - a) * b["rate"] + a * rate, b["n"] + 1
        self._save()
        return hint


mood = Mood()

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
    if ear.lock.locked() or ear.streaming:
        return web.json_response({"error": "busy"}, status=409)
    async with ear.lock:
        t0 = time.time()
        timings = {}
        card = await asyncio.to_thread(find_bt_card) if CONFIG.get("bluetooth", True) else None
        restore = None
        beep_sink = CONFIG.get("beep_sink") or None
        if card:
            prof = await asyncio.to_thread(card_profile, card)
            if "bap-duplex" in await asyncio.to_thread(card_profiles, card):
                # LE Audio: звук и микрофон одновременно — ничего не переключаем, начало ответа не теряется
                if prof != "bap-duplex":
                    await set_profile(card, "bap-duplex")
                    await asyncio.sleep(CONFIG.get("le_settle_s", 0.5))
                timings["mode"] = "le"
            elif prof != CONFIG.get("hfp_profile", "headset-head-unit"):
                restore = prof
                await set_profile(card, CONFIG.get("hfp_profile", "headset-head-unit"))
                await asyncio.sleep(CONFIG.get("hfp_settle_s", 0.6))
            if not beep_sink:
                beep_sink = await asyncio.to_thread(bt_node, card, "sinks") or \
                    "bluez_output." + card[len("bluez_card."):].replace("_", ":")
        source = await asyncio.to_thread(find_source, card)
        if not source:
            return web.json_response({"error": "no_microphone"}, status=503)
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
        spk_task = asyncio.create_task(asyncio.to_thread(ear.vp.check, pcm)) if ear.vp else None
        text, sure = await asyncio.to_thread(ear.transcribe_sure, pcm)
        timings["stt_s"] = round(time.time() - t1, 2)
        if sure and (sure["weak"] or sure["mean"] < CONFIG.get("asr_unsure_mean", -0.2)):
            timings["asr"] = sure
        m = mood.hint(pcm, text, "hfp" if restore else "le" if timings.get("mode") == "le" else "default")
        if m:
            timings["mood"] = m
        speaker = await spk_task if spk_task else {"owner": None, "enrolled": False}
        if restore_task:
            # ответ Ксении должен играть уже в A2DP: ждём конца переключения (идёт параллельно с распознаванием)
            timings["a2dp_ready_s"] = await restore_task
        timings["total_s"] = round(time.time() - t0, 2)
        log.info("Услышала (%s, голос %s): %s", timings, speaker, text)
        return web.json_response({"text": text, "timings": timings, "speaker": speaker})


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
        speaker = await asyncio.to_thread(ear.vp.check, audio) if ear.vp else {"owner": None, "enrolled": False}
    return web.json_response({"text": text, "stt_s": round(time.time() - t1, 2), "speaker": speaker})


async def handle_vp(request):
    """Отпечаток голоса: add_last — добавить последнюю реплику в образец, save, clear, status."""
    if not ear.vp:
        return web.json_response({"ok": False, "error": "модуль отпечатка не загружен"}, status=503)
    action = request.match_info["action"]
    if action == "add_last":
        return web.json_response(ear.vp.add_last())
    if action == "save":
        return web.json_response(ear.vp.save(CONFIG.get("enroll_min_phrases", 4)))
    if action == "clear":
        return web.json_response(ear.vp.clear())
    return web.json_response({"ok": True, "enrolled": ear.vp.centroid is not None, "collected": len(ear.vp.pending)})


async def handle_stream(request):
    """Живой режим: WebSocket с событиями слуха, пока ядро держит соединение. Только LE Audio
    (звук и микрофон сразу): в обычном Bluetooth пришлось бы держать наушники в режиме гарнитуры.
    События: ready, speech_start, speech_long, utterance {text, speaker, timings}, error {reason}."""
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    card = await asyncio.to_thread(find_bt_card) if CONFIG.get("bluetooth", True) else None
    if not card or "bap-duplex" not in await asyncio.to_thread(card_profiles, card):
        await ws.send_json({"type": "error", "reason": "not_duplex"})
        await ws.close()
        return ws
    if ear.lock.locked() or ear.streaming:
        await ws.send_json({"type": "error", "reason": "busy"})
        await ws.close()
        return ws
    if await asyncio.to_thread(card_profile, card) != "bap-duplex":
        await set_profile(card, "bap-duplex")
        await asyncio.sleep(CONFIG.get("le_settle_s", 0.5))
    source = await asyncio.to_thread(find_source, card)
    sink = await asyncio.to_thread(bt_node, card, "sinks")
    ear.streaming = True
    rec = None
    try:
        if sink and CONFIG.get("beep", True):
            p = await asyncio.create_subprocess_exec(
                "pacat", "--playback", "-d", sink, "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le",
                "--latency-msec=30", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            p.stdin.write(BEEP)
            await p.stdin.drain()
            p.stdin.close()
            await p.wait()
        rec = await asyncio.create_subprocess_exec(
            "parec", "-d", source, "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le",
            "--latency-msec=20", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        seg = LiveSegmenter()

        async def reader():
            # служебные кадры (ping/pong, закрытие) aiohttp обрабатывает только внутри receive():
            # без чтения сердцебиение не проходило, и поток рвался через полторы минуты (живой тест)
            async for msg in ws:
                # ядро сообщает, что делает Ксения: говорит / думает / задала вопрос — для конца реплики
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        m = json.loads(msg.data)
                    except ValueError:
                        continue
                    if m.get("type") == "context":
                        seg.ctx.update({k: m[k] for k in ("ksenia", "asked") if k in m})
        reader_task = asyncio.create_task(reader())
        partial = {"task": None, "utt": 0}

        async def send_partial(pcm, utt_no):
            try:
                text = await asyncio.to_thread(ear.transcribe, pcm)
            except Exception as e:
                log.warning("частичное распознавание: %r", e)
                return
            # чей голос — один раз на реплику, как только набралась секунда: «подожди» гостя не должно
            # останавливать Ксению (совет Fable, REVIEW-3 п. 10.8); без образца голоса — None
            owner = partial.get("owner") if partial.get("owner_utt") == utt_no else None
            if owner is None and ear.vp is not None and ear.vp.centroid is not None and len(pcm) >= RATE:
                try:
                    owner = (await asyncio.to_thread(ear.vp.check, pcm)).get("owner")
                    partial.update({"owner": owner, "owner_utt": utt_no})
                except Exception:
                    owner = None
            if text and utt_no == partial["utt"] and not ws.closed:
                await ws.send_json({"type": "partial", "text": text, "speech_s": round(len(pcm) / RATE, 2),
                                    "owner": owner})
        await ws.send_json({"type": "ready", "source": source})
        log.info("Живой режим: микрофон открыт (%s)", source)
        while not ws.closed:
            try:
                buf = await asyncio.wait_for(rec.stdout.readexactly(FRAME * 2), timeout=3)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                log.warning("Живой режим: микрофон оборвался: %r", e)
                await ws.send_json({"type": "error", "reason": "mic_lost"})
                if headset:  # канал LE Audio мог не подняться — проверить и починить в фоне
                    asyncio.create_task(headset.check_and_recover("микрофон оборвался"))
                break
            ev = seg.push(np.frombuffer(buf, dtype=np.int16))
            if ev == "partial":
                if partial["task"] is None or partial["task"].done():  # одно распознавание за раз
                    partial["task"] = asyncio.create_task(send_partial(seg.pcm(), partial["utt"]))
                ev = None
            elif ev == "check_fast":
                # быстрая проверка (~0,4 с): только текст, конец — лишь при ясных признаках
                text = await asyncio.to_thread(ear.transcribe, seg.pcm())
                ev = seg.decide(1.0, text, fast=True)
            elif ev == "check":
                ev = seg.decide(*await asyncio.to_thread(ear.turn_check, seg.pcm()))
                if ev is None:  # пауза, но он не договорил — ядро может ответить своим «угу»
                    await ws.send_json({"type": "pause", "speech_s": round(seg.voiced_ms / 1000, 1)})
            if ev == "start":
                await ws.send_json({"type": "speech_start"})
            elif ev == "long":
                await ws.send_json({"type": "speech_long"})
            elif ev == "end":
                partial["utt"] += 1  # опоздавшие частичные результаты этой реплики больше не нужны
                pcm, info = seg.utterance()
                t1 = time.time()
                ear._save_debug([pcm])
                spk = asyncio.create_task(asyncio.to_thread(ear.vp.check, pcm)) if ear.vp else None
                text, sure = await asyncio.to_thread(ear.transcribe_sure, pcm)
                info["stt_s"] = round(time.time() - t1, 2)
                if sure and (sure["weak"] or sure["mean"] < CONFIG.get("asr_unsure_mean", -0.2)):
                    info["asr"] = sure
                m = mood.hint(pcm, text, "le")
                if m:
                    info["mood"] = m
                speaker = await spk if spk else {"owner": None, "enrolled": False}
                log.info("Живой режим, услышала (%s, голос %s): %s", info, speaker, text)
                await ws.send_json({"type": "utterance", "text": text, "speaker": speaker, "timings": info})
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        ear.streaming = False
        if "reader_task" in locals():
            reader_task.cancel()
        if rec and rec.returncode is None:
            rec.kill()
            await rec.wait()
        log.info("Живой режим: микрофон закрыт")
        if not ws.closed:
            await ws.close()
    return ws


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


headset = None


async def announce(text):
    """Служебная фраза голосом Ксении (мимо мозга): «Наушники переподключила…»."""
    p = await asyncio.create_subprocess_exec(os.path.join(ROOT, "..", "scripts", "ksenia-announce"), text,
                                             stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await p.wait()


async def handle_headset(request):
    """GET /headset — состояние; POST /headset/check — проверить и починить канал; POST /headset/mode/{music|talk}."""
    action = request.match_info.get("action", "status")
    if request.method == "GET" or action == "status":
        return web.json_response(await headset.status())
    if action == "check":
        return web.json_response(await headset.check_and_recover("просьба ядра"))
    if action == "mode":
        return web.json_response(await headset.set_mode(request.match_info.get("mode", "")))
    return web.json_response({"ok": False, "error": "неизвестное действие"}, status=404)


async def _headset_start(app):
    async def first_check():
        await asyncio.sleep(CONFIG.get("headset_check_delay_s", 8))  # наушники могут подключаться после слуха
        res = await headset.check_and_recover("запуск слуха")
        log.info("Наушники при запуске: %s", res)
    app["headset_tasks"] = [asyncio.create_task(first_check()), asyncio.create_task(headset.watch())]


async def _headset_stop(app):
    for t in app.get("headset_tasks", []):
        t.cancel()


def main():
    global ear, headset
    ear = Ear()
    import headset as headset_mod
    headset = headset_mod.Headset(CONFIG, say=announce)
    app = web.Application(client_max_size=50 * 1024 * 1024, middlewares=[local_only])
    app.add_routes([web.post("/listen", handle_listen), web.post("/transcribe", handle_transcribe),
                    web.post("/voiceprint/{action}", handle_vp),
                    web.get("/status", handle_status), web.get("/stream", handle_stream),
                    web.get("/headset", handle_headset), web.post("/headset/{action}", handle_headset),
                    web.post("/headset/{action}/{mode}", handle_headset)])
    if CONFIG.get("headset_guard", True):
        app.on_startup.append(_headset_start)
        app.on_cleanup.append(_headset_stop)
    web.run_app(app, host="127.0.0.1", port=CONFIG.get("port", 18120), print=None)


if __name__ == "__main__":
    main()
