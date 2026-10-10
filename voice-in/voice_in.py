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
    в LE Audio — «bluez_input.88_92_….0». kind: "sinks" | "sources".
    Наушники бывают подключены сразу обоими каналами — тогда в списке оба узла, а звук идёт только через узел
    включённого профиля; молчащий узел обычного Bluetooth стоял первым, и живой разговор слушал тишину
    (живой тест 2026-10-10)."""
    mac = card[len("bluez_card."):]
    prefix = "bluez_output." if kind == "sinks" else "bluez_input."
    found = []
    for line in sh("pactl", "list", kind, "short").splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and parts[1].startswith(prefix) and not parts[1].endswith(".monitor") \
                and mac.replace("_", "") in parts[1].replace("_", "").replace(":", ""):
            found.append(parts[1])
    if len(found) > 1:
        le = (card_profile(card) or "").startswith("bap")
        found.sort(key=lambda n: (":" in n) == le)  # LE Audio — имя с подчёркиваниями, обычный — с двоеточиями
    return found[0] if found else None


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

# громкость 0.3 была резкой в ушах (живой тест 2026-10-10) — по умолчанию втрое тише
BEEP = b"\x00\x00" * int(RATE * 0.35) + make_beep(freq=880, ms=200, vol=CONFIG.get("beep_volume", 0.1))


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
    if not t or t[-1] in "?!":  # «Что?», «Давай!» — законченный вопрос или ответ (аудит Fable, D8)
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
    words = len(re.findall(r"\w+", t))
    # сначала явный конец: «Давай.» в ответ на вопрос Ксении ждал 3 с из-за «висящего» «давай» (аудит Fable, D8)
    strong = bool(t) and (t.endswith(("?", "!")) or (ctx.get("asked") and bool(SHORT_ANSWER.match(t))))
    if strong:
        return "end", None
    if t and hanging(t):
        return "wait", cfg.get("turn_hang_wait_ms", 3000)
    if fast:
        return "wait", None
    if p >= cfg.get("turn_threshold", 0.5) and (words or not t):
        return "end", None
    return "wait", cfg.get("turn_wait_ms", 2000)


_BG = set()


def spawn_bg(coro):
    """Фоновая задача со ссылкой: цикл событий держит задачи слабо (аудит Fable, D21)."""
    t = asyncio.get_running_loop().create_task(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)
    return t


async def infer(fn, *args):
    """Распознавание/отпечаток в отдельном потоке, но не дольше 15 с. Зависшая CUDA (после сна) держала замок
    слуха навсегда — «Я ещё дослушиваю» без конца; сломанная — 500 на каждую реплику при живом процессе.
    Тогда лучше выйти: systemd перезапустит слух (аудит Fable, D1)."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), CONFIG.get("infer_timeout_s", 15))
    except asyncio.TimeoutError:
        log.critical("Распознавание зависло дольше %s с — перезапускаюсь", CONFIG.get("infer_timeout_s", 15))
        os._exit(1)
    except Exception as e:
        if re.search(r"cuda|cudnn|cublas|CUDNN|CUBLAS|device", repr(e), re.I):
            log.critical("Сбой видеокарты в распознавании (%r) — перезапускаюсь", e)
            os._exit(1)
        raise


class Ear:
    def __init__(self):
        t0 = time.time()
        # Движок распознавания: GigaAM v3 e2e-CTC (по замерам 2026-10-08: та же точность 4,0%, в 11 раз быстрее
        # Whisper, CTC не «выдумывает» текст на шуме) или Whisper (запасной). Переключение — config.json "engine".
        self.engine = CONFIG.get("engine", "gigaam")
        if self.engine == "gigaam":
            import onnx_asr
            # asr_device: "cuda" (по умолчанию) или "cpu" — GigaAM лёгкая, на процессоре реплика ~0,1 с;
            # процессор — запасной путь, если видеокарта голоса и слуха недоступна (зависший процесс держит её)
            providers = ["CPUExecutionProvider"] if CONFIG.get("asr_device") == "cpu" else \
                ["CUDAExecutionProvider", "CPUExecutionProvider"]
            try:
                self.model = onnx_asr.load_model(CONFIG.get("gigaam_model", "gigaam-v3-e2e-ctc"), providers=providers)
                self.model.recognize(np.zeros(RATE, dtype=np.float32), sample_rate=RATE)  # прогрев
            except Exception as e:
                # видеокарта недоступна (после сна, зависший процесс): слышать на процессоре лучше, чем падать
                # каждые 10 с (аудит Fable, D1)
                log.error("Распознавание на видеокарте не запустилось (%r) — работаю на процессоре", e)
                self.model = onnx_asr.load_model(CONFIG.get("gigaam_model", "gigaam-v3-e2e-ctc"),
                                                 providers=["CPUExecutionProvider"])
                self.model.recognize(np.zeros(RATE, dtype=np.float32), sample_rate=RATE)
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
            try:
                # выход наушников без звукового канала — pacat висел вечно, держа замок слуха (аудит Fable, D4)
                await asyncio.wait_for(p.wait(), 3)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
                log.warning("Сигнал не проигрался за 3 с — выход наушников не отвечает")
        rec = await asyncio.create_subprocess_exec(
            "parec", "-d", source, "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le",
            "--latency-msec=20", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        frames, speech_started, silent_ms, noise, voiced_win = [], False, 0, None, []
        smart = CONFIG.get("smart_turn", True)
        turn_checked, turn_probs, turn_texts = False, [], []
        wait_ms = CONFIG.get("turn_wait_ms", 2000)
        t_start = time.time()
        t_speech, speech_frame = None, 0
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
                        speech_frame = max(0, len(frames) - 30)  # окно речи (до 300 мс) + 300 мс запаса
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
                        p, partial = await infer(self.turn_check, np.concatenate(frames))
                        turn_probs.append(round(p, 2))
                        if partial:
                            turn_texts.append(partial[-40:])
                        hang = hanging(partial)
                        if partial.strip().endswith(("?", "!")) or (p >= CONFIG.get("turn_threshold", 0.5) and not hang):
                            break
                        wait_ms = CONFIG.get("turn_hang_wait_ms", 3000) if hang else CONFIG.get("turn_wait_ms", 2000)
                    if silent_ms >= (wait_ms if smart else silence_ms):
                        break
        finally:
            rec.kill()
            await rec.wait()
        self._save_debug(frames)
        # тишина до начала речи (до 12 с после сигнала) не нужна ни распознаванию, ни отпечатку голоса: отпечаток
        # брал середину записи и слышал в основном тишину (аудит Fable, D3)
        pcm = np.concatenate(frames[speech_frame:])
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
        # предел длины — на каждом кадре: ровный фон (телевизор, чайник, пылесос) после фразы держал реплику
        # открытой без конца — ответа не было вовсе (аудит Fable, D6)
        if len(self.frames) * 20 >= self.c.get("live_max_s", 45) * 1000:
            return "end"
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
            profiles = await asyncio.to_thread(card_profiles, card)
            if "bap-duplex" in profiles:
                # LE Audio: звук и микрофон одновременно — ничего не переключаем, начало ответа не теряется
                if prof != "bap-duplex":
                    restore = prof if (prof or "").startswith("a2dp") else None  # музыка — вернуть после записи
                    await set_profile(card, "bap-duplex")
                    await asyncio.sleep(CONFIG.get("le_settle_s", 0.5))
                timings["mode"] = "le"
            elif prof != CONFIG.get("hfp_profile", "headset-head-unit"):
                restore = prof
                await set_profile(card, CONFIG.get("hfp_profile", "headset-head-unit"))
                await asyncio.sleep(CONFIG.get("hfp_settle_s", 0.6))
            else:
                # наушники уже в режиме гарнитуры — прошлую запись оборвали (перезапуск слуха): после этой записи
                # вернуть хороший звук, иначе голос и музыка остались бы «телефонными» навсегда (аудит Fable, D10)
                a2dp = [x for x in profiles if x.startswith("a2dp")]
                best = ("ldac", "aptx_hd", "aptx", "aac", "sbc_xq", "sbc")
                restore = min(a2dp, key=lambda x: next((i for i, c in enumerate(best) if c in x), len(best)),
                              default=None)
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
        spk_task = asyncio.create_task(infer(ear.vp.check, pcm)) if ear.vp else None
        text, sure = await infer(ear.transcribe_sure, pcm)
        timings["stt_s"] = round(time.time() - t1, 2)
        if sure and (sure["weak"] or sure["mean"] < CONFIG.get("asr_unsure_mean", -0.2)):
            timings["asr"] = sure
        m = mood.hint(pcm, text, "le" if timings.get("mode") == "le" else "hfp" if restore else "default")
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
        text = await infer(ear.transcribe, audio)
        speaker = await infer(ear.vp.check, audio) if ear.vp else {"owner": None, "enrolled": False}
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
    if action == "begin":  # новая запись образца: обрывки прошлой, недописанной, не подмешиваем (аудит Fable, D13)
        ear.vp.pending = []
        return web.json_response({"ok": True, "collected": 0})
    return web.json_response({"ok": True, "enrolled": ear.vp.centroid is not None, "collected": len(ear.vp.pending)})


class PushPipe:
    """Звук с планшета (живой разговор через шлюз pwa/): кадры PCM 16 кГц приходят по WebSocket /push,
    живой режим читает их так же, как вывод parec с микрофона наушников (readexactly)."""

    def __init__(self):
        self.q = asyncio.Queue(maxsize=1000)
        self.buf = b""
        self.closed = False

    def put(self, data: bytes):
        if not self.closed:
            try:
                self.q.put_nowait(data)
            except asyncio.QueueFull:
                pass  # слух не успевает — лучше потерять кадр, чем копить задержку

    def close(self):
        self.closed = True
        try:
            self.q.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def readexactly(self, n):
        while len(self.buf) < n:
            chunk = await self.q.get()
            if chunk is None:
                raise asyncio.IncompleteReadError(self.buf, n)
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


PUSH = {"pipe": None}


async def handle_push(request):
    """Шлюз планшета присылает сюда звук микрофона (PCM s16le, 16 кГц, моно) для живого разговора."""
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)
    if PUSH["pipe"] is not None:
        PUSH["pipe"].close()
    pipe = PUSH["pipe"] = PushPipe()
    log.info("Живой разговор с планшета: звук пошёл")
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                pipe.put(msg.data)
    finally:
        pipe.close()
        if PUSH["pipe"] is pipe:
            PUSH["pipe"] = None
        log.info("Живой разговор с планшета: звук закончился")
    return ws


async def handle_stream(request):
    """Живой режим: WebSocket с событиями слуха, пока ядро держит соединение. Только LE Audio
    (звук и микрофон сразу): в обычном Bluetooth пришлось бы держать наушники в режиме гарнитуры.
    События: ready, speech_start, speech_long, utterance {text, speaker, timings}, error {reason}."""
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    push = request.query.get("source") == "push"
    back = request.query.get("restore") == "1"  # позвали касанием в режиме музыки — после разговора вернуть LDAC
    if ear.lock.locked() or ear.streaming:
        await ws.send_json({"type": "error", "reason": "busy"})
        await ws.close()
        return ws
    ear.streaming = True  # сразу: второй /stream во время подготовки запускался параллельно (аудит Fable, D16)
    try:
        return await _stream(ws, push, back)
    finally:
        ear.streaming = False


async def _stream(ws, push, back=False):
    card, restore = None, None
    if push:
        # звук с планшета: эхо гасит его браузер, сигнал «слушаю» звучит на самом планшете
        for _ in range(50):
            if PUSH["pipe"] is not None:
                break
            await asyncio.sleep(0.2)
        if PUSH["pipe"] is None:
            await ws.send_json({"type": "error", "reason": "no_push_audio"})
            await ws.close()
            return ws
        source, sink = "планшет", None
    else:
        card = await asyncio.to_thread(find_bt_card) if CONFIG.get("bluetooth", True) else None
        if not card or "bap-duplex" not in await asyncio.to_thread(card_profiles, card):
            await ws.send_json({"type": "error", "reason": "not_duplex"})
            await ws.close()
            return ws
        prof = await asyncio.to_thread(card_profile, card)
        if prof != "bap-duplex":
            restore = prof if back and (prof or "").startswith("a2dp") else None
            await set_profile(card, "bap-duplex")
            await asyncio.sleep(CONFIG.get("le_settle_s", 0.5))
        source = await asyncio.to_thread(find_source, card)
        sink = await asyncio.to_thread(bt_node, card, "sinks")
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
            try:
                await asyncio.wait_for(p.wait(), 3)  # выход без звукового канала — pacat висел вечно (D4)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
        if push:
            mic = PUSH["pipe"]
        else:
            rec = await asyncio.create_subprocess_exec(
                "parec", "-d", source, "--raw", f"--rate={RATE}", "--channels=1", "--format=s16le",
                "--latency-msec=20", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            mic = rec.stdout
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
                text = await infer(ear.transcribe, pcm)
            except Exception as e:
                log.warning("частичное распознавание: %r", e)
                return
            # чей голос — один раз на реплику, как только набралась секунда: «подожди» гостя не должно
            # останавливать Ксению (совет Fable, REVIEW-3 п. 10.8); без образца голоса — None
            owner = partial.get("owner") if partial.get("owner_utt") == utt_no else None
            if owner is None and ear.vp is not None and ear.vp.centroid is not None and len(pcm) >= RATE:
                try:
                    owner = (await infer(ear.vp.check, pcm)).get("owner")
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
                # звук с планшета идёт по Wi-Fi — запас на заминки сети больше, чем у микрофона наушников
                buf = await asyncio.wait_for(mic.readexactly(FRAME * 2), timeout=10 if push else 3)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
                log.warning("Живой режим: микрофон оборвался: %r", e)
                await ws.send_json({"type": "error", "reason": "push_lost" if push else "mic_lost"})
                if headset and not push:  # канал LE Audio мог не подняться — проверить и починить в фоне
                    spawn_bg(headset.check_and_recover("микрофон оборвался"))
                break
            try:
                await _live_event(ws, seg, partial, send_partial, np.frombuffer(buf, dtype=np.int16))
            except (ConnectionResetError, asyncio.CancelledError):
                raise
            except Exception as e:
                # раньше любое исключение тихо закрывало поток — живой режим кончался молча (аудит Fable, D5)
                log.exception("Живой режим: сбой обработки реплики")
                if not ws.closed:
                    await ws.send_json({"type": "error", "reason": "asr_failed", "detail": repr(e)[:200]})
                seg.reset()
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        if "reader_task" in locals():
            reader_task.cancel()
        if rec and rec.returncode is None:
            rec.kill()
            await rec.wait()
        log.info("Живой режим: микрофон закрыт")
        if restore:
            # наушники одновременно в LE Audio и обычном Bluetooth: смена профиля — доли секунды, без переподключения
            log.info("Живой режим: возвращаю музыку (%s)", restore)
            await restore_a2dp(card, restore)
        if not ws.closed:
            await ws.close()
    return ws


async def _live_event(ws, seg, partial, send_partial, frame):
    """Один кадр живого потока: события начала, паузы, конца реплики."""
    ev = seg.push(frame)
    if ev == "partial":
        if partial["task"] is None or partial["task"].done():  # одно распознавание за раз
            partial["task"] = asyncio.create_task(send_partial(seg.pcm(), partial["utt"]))
        ev = None
    elif ev == "check_fast":
        # быстрая проверка (~0,4 с): только текст, конец — лишь при ясных признаках
        text = await infer(ear.transcribe, seg.pcm())
        ev = seg.decide(1.0, text, fast=True)
    elif ev == "check":
        ev = seg.decide(*await infer(ear.turn_check, seg.pcm()))
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
        spk = asyncio.create_task(infer(ear.vp.check, pcm)) if ear.vp else None
        text, sure = await infer(ear.transcribe_sure, pcm)
        info["stt_s"] = round(time.time() - t1, 2)
        if sure and (sure["weak"] or sure["mean"] < CONFIG.get("asr_unsure_mean", -0.2)):
            info["asr"] = sure
        m = mood.hint(pcm, text, "le")
        if m:
            info["mood"] = m
        try:
            speaker = await spk if spk else {"owner": None, "enrolled": False}
        except Exception:
            speaker = {"owner": None, "enrolled": True}  # не проверили — неизвестный голос, не «гость»
        log.info("Живой режим, услышала (%s, голос %s): %s", info, speaker, text)
        await ws.send_json({"type": "utterance", "text": text, "speaker": speaker, "timings": info})


async def handle_status(request):
    # pactl — в потоке и без падения: синхронный вызов держал весь слух до 15 с (аудит Fable, D15)
    def snapshot():
        card = find_bt_card()
        return (card, (card_profile(card) if card else None), find_source(card),
                card is not None and "bap-duplex" in card_profiles(card))
    try:
        card, prof, src, duplex = await asyncio.wait_for(asyncio.to_thread(snapshot), 8)
    except Exception:
        card = prof = src = duplex = None
    # duplex: живой разговор возможен и из режима музыки — слух сам переключит профиль на время разговора
    return web.json_response({"busy": ear.lock.locked() or ear.streaming, "bt_card": card, "profile": prof,
                              "source": src, "duplex": bool(duplex)})


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
    # браузер к слуху не ходит (звук планшета — через шлюз, без Origin): любой Origin — отказ (аудит Fable, A2)
    origin = request.headers.get("Origin")
    if _hostname(request.headers.get("Host", "")) not in LOCAL_HOSTS or origin is not None:
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


def no_wp_autoswitch():
    """Профилями наушников управляет слух. Автопереключение WirePlumber не видит, что Ксения слушает микрофон
    LE Audio, и через 2 с возвращало наушники в режим музыки — живой разговор слышал только первую фразу
    (живой тест 2026-10-10). Выключаем при каждом запуске: настройка не должна держаться на памяти агента."""
    try:
        if "true" in sh("wpctl", "settings", "bluetooth.autoswitch-to-headset-profile").lower():
            sh("wpctl", "settings", "--save", "bluetooth.autoswitch-to-headset-profile", "false")
            log.info("WirePlumber: автопереключение профиля наушников выключено")
    except Exception as e:
        log.warning("WirePlumber: не удалось выключить автопереключение: %r", e)


async def _headset_start(app):
    await asyncio.to_thread(no_wp_autoswitch)
    async def first_check():
        await asyncio.sleep(CONFIG.get("headset_check_delay_s", 8))  # наушники могут подключаться после слуха
        res = await headset.check_and_recover("запуск слуха")
        log.info("Наушники при запуске: %s", res)
    app["headset_tasks"] = [asyncio.create_task(first_check()), asyncio.create_task(headset.watch())]


async def _headset_stop(app):
    for t in app.get("headset_tasks", []):
        t.cancel()


def _exit_now(*_):
    """Остановка службы: выйти сразу, без разбора моделей. Выгрузка onnxruntime (CUDA) при выходе однажды
    зависла внутри библиотеки (поток в состоянии R не убивался даже SIGKILL — 2026-10-09), и systemd
    не мог перезапустить слух; GPU-память освобождает драйвер при завершении процесса."""
    logging.shutdown()
    os._exit(0)


def main():
    global ear, headset
    import signal
    signal.signal(signal.SIGTERM, _exit_now)
    ear = Ear()
    import headset as headset_mod
    headset = headset_mod.Headset(CONFIG, say=announce)
    app = web.Application(client_max_size=50 * 1024 * 1024, middlewares=[local_only])
    app.add_routes([web.post("/listen", handle_listen), web.post("/transcribe", handle_transcribe),
                    web.post("/voiceprint/{action}", handle_vp),
                    web.get("/status", handle_status), web.get("/stream", handle_stream), web.get("/push", handle_push),
                    web.get("/headset", handle_headset), web.post("/headset/{action}", handle_headset),
                    web.post("/headset/{action}/{mode}", handle_headset)])
    if CONFIG.get("headset_guard", True):
        app.on_startup.append(_headset_start)
        app.on_cleanup.append(_headset_stop)
    web.run_app(app, host="127.0.0.1", port=CONFIG.get("port", 18120), print=None, handle_signals=False)


if __name__ == "__main__":
    main()
