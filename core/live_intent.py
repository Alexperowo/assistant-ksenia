"""Что значит реплика Александра, сказанная, пока Ксения говорит или думает (живой режим).

Решение по смыслу и контексту, а не по словарю: важно, ЧТО Ксения сейчас делает (рассказывает, думает, задала
вопрос) и КАК устроена реплика (вопрос ли, просьба ли, только реакция ли, с чего начинается). Три ступени:

1. Мгновенно (< 1 мс): ясные случаи по строению реплики и контексту. Короткая реакция без своего содержания
   («ничего себе», «правда?», «а дальше?») во время рассказа — продолжать; «подожди…», «стоп» в начале —
   замолчать; длинная реплика со своим содержанием или вопрос — перебить и ответить.
2. Судья (~50–300 мс): спорные случаи — короткий запрос к языковой модели с контекстом (что Ксения сказала
   последним, рассказывает ли, о чём реплика). По умолчанию — вторая ячейка мозга (id_slot=1, как зрение);
   лучше — маленькая модель на RTX 2080 Ti (live_judge_url). Пока судья думает, Ксения говорит дальше в полный голос.
3. Запасной путь: прежний словарь — если судьи нет или он не успел.

Слова в списках ниже — не перечень допустимых фраз, а признаки строения реплики (вопросительные слова,
служебные слова-команды). Всё, что ими не решается однозначно, решает судья.

Каждое решение пишется в data/live_decisions.jsonl (только на этом компьютере): из них потом можно обучить
маленький классификатор (ruBERT-tiny2, ~4 мс на CPU) на настоящих репликах Александра.
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger("core")

# что делать с речью Ксении
KINDS = {
    "continue": "поддакивание, реакция или просьба продолжать — говорить дальше",
    "hold": "«подожди», «секунду» — замолчать и ждать",
    "stop": "«стоп», «хватит» — замолчать совсем",
    "question": "вопрос по теме — перебить и ответить",
    "request": "новая просьба или другая тема — перебить и ответить",
    "correction": "поправка, возражение — перебить и ответить",
    "goodbye": "прощание — ответить и закончить",
    "aside": "говорит не с Ксенией (телефон, кто-то рядом) — замолчать и ждать",
    "reaction": "короткая реакция на законченный ответ — ответить совсем коротко",
    "noise": "не слова — ничего не делать",
}
INTERRUPTING = {"hold", "stop", "question", "request", "correction", "goodbye", "aside"}


def norm(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    # распознавание теряет начало «стоп»: «Stop», «Сtop», «Хопп», «Хопер» (живые тесты 2026-10-08)
    t = re.sub(r"\b(?:[сc]?stop|[сc]top)\b", "стоп", t)
    return " ".join(re.findall(r"\w+", t))  # «ха-ха», «да-да», «э-э» — отдельными словами


def stop_like(word: str) -> bool:
    """Одно короткое слово, похожее на «стоп» с потерянным началом: «хоп», «хопп», «топ», «хопер»."""
    return bool(re.fullmatch(r"(?:с|ш|х|т|к|ст|сх|хт)?[оa]п{1,2}(?:ер)?", word))


# признаки строения реплики
HOLD_W = set("подожди погоди постой стой секунду секундочку минутку минуточку минуту момент тихо тише".split())
STOP_W = set("стоп хватит замолчи замолкни отбой достаточно прекрати".split())
LEAD_W = set("ой так ну эй слушай ксения а".split())  # могут стоять перед «подожди»: «ой, подожди»
INTERROG = set("что кто где когда куда откуда почему зачем как какой какая какое какие каких каком какую "
               "сколько чей чья чье чьи ли разве".split())
CORRECT_HEAD = ("нет не", "не так", "я не это", "я не про", "я имел в виду", "я имела в виду", "я сказал",
                "не то", "ты не поняла", "неправильно", "наоборот")
ASIDE_HEAD = ("алло", "да алло", "алло да", "слушаю", "да слушаю")
# реакции слушателя — слова без своего содержания (оценка, удивление, согласие, «продолжай»)
REACT_W = set("""ага угу да да-да ну так хм м мм ммм ок окей понятно ясно понял поняла понимаю интересно интересненько
круто класс классно здорово отлично супер вау ого ох ух ты ничего себе вот это надо же серьезно правда правильно
верно конечно хорошо ладно прикольно забавно обалдеть офигеть ясненько угу-угу ага-ага реально точно неужели
да ладно не может быть ха хаха ахах смешно жесть кошмар ужас жаль грустно красиво
рассказывай рассказывай-рассказывай продолжай дальше давай слушаю внимательно я тебя буду слушать еще же
а и потом что нет не ксения""".replace("ё", "е").split())
CONT_W = set("продолжай рассказывай дальше потом давай слушаю".split())
# привлечь внимание или подбирать слова: «слушай…», «эй», «э-э», «ну» — сейчас что-то скажет, ждать
ATTN_W = set("слушай эй смотри знаешь короче так ну э ээ эээ э-э эм мм хм".split())


def _head(words):
    """Служебное начало реплики: «ой, подожди», «Ксения, стоп», «так, погоди» -> (начало, остальное)."""
    i, cmd = 0, False
    while i < len(words) and i < 4:
        w = words[i]
        if w in HOLD_W or w in STOP_W:
            cmd = True
        elif not (w in LEAD_W or (cmd and w in ("ксения", "секунду", "минутку"))):
            break
        i += 1
    return (words[:i], words[i:]) if cmd else ([], words)


@dataclass
class Context:
    state: str = "idle"          # speaking | thinking | idle — что делает Ксения
    said: str = ""               # что Ксения уже сказала в этом ответе (или последний ответ)
    asked: bool = False          # её последняя фраза — вопрос Александру
    story: bool = False          # идёт длинный рассказ
    interrupted: bool = False    # есть недосказанный рассказ, к которому можно вернуться
    partial: bool = False        # реплика ещё не договорена (частичное распознавание)


@dataclass
class Decision:
    kind: str
    sure: bool
    source: str = "rules"
    why: str = ""
    ms: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def interrupts(self):
        return self.kind in INTERRUPTING


def quick(text: str, ctx: Context) -> Decision:
    """Мгновенное решение по строению реплики и контексту. sure=False — нужен судья."""
    raw = (text or "").strip()
    words = norm(raw).split()
    if not words or (len(words) == 1 and len(words[0]) < 2):
        return Decision("noise", True, why="пусто")
    t = " ".join(words)
    busy = ctx.state != "idle"
    asks = raw.endswith("?")

    if busy and len(words) <= 2 and any(stop_like(w) for w in words) and not any(w in REACT_W for w in words):
        return Decision("stop", True, why="похоже на «стоп» с потерянным началом")
    if any(t == a or t.startswith(a + " ") for a in ASIDE_HEAD):
        return Decision("aside", True, why="телефон")
    if words == ["ксения"]:
        # позвал по имени: во время речи — замолчать и слушать, в тишине — откликнуться («Да?»)
        return Decision("hold" if busy else "request", True, why="позвал по имени")
    if all(w in ATTN_W or w == "ксения" for w in words):
        return Decision("hold", True, why="зовёт или подбирает слова — сейчас скажет")
    head, rest = _head(words)
    if head and not rest:
        return Decision("stop" if any(w in STOP_W for w in head) else "hold", True, why="команда без продолжения")
    if _goodbye(t):
        return Decision("goodbye", True, why="прощание")
    if any(t == c or t.startswith(c + " ") for c in CORRECT_HEAD):
        return Decision("correction", True, why="поправка")
    content = [w for w in rest if w not in REACT_W]
    question = asks or bool(rest and rest[0] in INTERROG) or (len(rest) > 1 and rest[1] == "ли") or \
        bool(len(rest) > 1 and rest[0] in ("а", "и") and rest[1] in INTERROG)
    if not content and len(rest) <= 12:
        # только реакции: «ничего себе», «правда?», «а дальше?», «ага, продолжай»
        if busy:
            return Decision("continue", True, why="реакция во время речи")
        if ctx.interrupted and any(w in CONT_W for w in rest):
            return Decision("continue", True, why="просит продолжить недосказанное")
        return Decision("reaction", True, why="реакция на законченный ответ")
    if head:
        # «подожди, а как звали лисичку?» — явно хочет сказать своё
        return Decision("question" if question else "request", True, why="команда и своё содержание")
    if len(content) >= 4 or len(rest) >= 6:
        return Decision("question" if question else "request", True, why="длинная реплика со своим содержанием")
    if question and content and not ctx.partial:
        return Decision("question", True, why="вопрос со своим содержанием")
    if not busy:
        return Decision("question" if question else "request", True, why="Ксения молчит — это реплика")
    return Decision("question" if question else "request", False, why="спорно: короткая реплика во время речи")


def _goodbye(t: str) -> bool:
    byes = ("пока", "до свидания", "спокойной ночи", "отбой", "всего доброго", "до завтра")
    n = len(t.split())
    return any(t == b or t.endswith(" " + b) or (t.startswith(b + " ") and n <= len(b.split()) + 2) for b in byes)


# ---------- судья ----------

JUDGE_CODES = {"A": "continue", "B": "hold", "C": "stop", "D": "question", "E": "request", "F": "correction",
               "G": "goodbye", "H": "aside"}
JUDGE_SYSTEM = (
    "Ты помогаешь голосовой собеседнице Ксении понять, что значит реплика Александра, сказанная, пока она говорит "
    "или думает. Ответь одной буквой:\n"
    "A — слушает и реагирует: поддакивание, удивление, оценка, «правда?», «а дальше?», «продолжай» (Ксения говорит дальше)\n"
    "B — просит подождать или отвлёкся: «подожди», «секунду» (Ксения замолкает и ждёт)\n"
    "C — просит замолчать совсем\n"
    "D — вопрос по теме, на который нужно ответить сейчас\n"
    "E — новая просьба или другая тема\n"
    "F — поправка или возражение («нет, я не про это»)\n"
    "G — прощается\n"
    "H — говорит не с Ксенией (по телефону или с кем-то рядом)\n"
    "Реплика пришла через распознавание речи и может быть искажена или не дослушана. Сомневаешься между A и "
    "остальными — выбирай A, если в реплике нет своего содержания; иначе — не A.")


def judge_prompt(text: str, ctx: Context) -> list:
    what = {"speaking": "говорит" + (" (длинный рассказ)" if ctx.story else ""), "thinking": "думает над ответом",
            "idle": "молчит"}.get(ctx.state, ctx.state)
    said = " ".join(ctx.said.split())[-240:]
    lines = [f"Ксения сейчас {what}."]
    if said:
        lines.append(f"Последнее, что она сказала: «…{said}»" + (" — это был вопрос Александру." if ctx.asked else ""))
    lines.append(f"Александр сказал{' (ещё говорит)' if ctx.partial else ''}: «{text.strip()}»")
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": "\n".join(lines) + "\nБуква:"}]


def parse_judge(answer: str):
    m = re.search(r"[A-H]", (answer or "").upper())
    return JUDGE_CODES.get(m.group(0)) if m else None


class Judge:
    """Короткий запрос к языковой модели (OpenAI-совместимый llama-server), ответ — одна буква.
    Результаты запоминаются: частичная реплика, проверенная заранее, к концу фразы уже решена."""

    def __init__(self, cfg: dict, key: str = ""):
        self.cfg, self.key = cfg, key
        self.cache = {}

    @property
    def enabled(self):
        return bool(self.cfg.get("live_judge", True)) and bool(self.url)

    @property
    def url(self):
        return self.cfg.get("live_judge_url") or self.cfg.get("brain_url")

    def _key(self, text, ctx):
        return (norm(text), ctx.state, ctx.asked, ctx.story)

    def cached(self, text, ctx):
        return self.cache.get(self._key(text, ctx))

    async def ask(self, session, text: str, ctx: Context):
        k = self._key(text, ctx)
        if k in self.cache:
            return self.cache[k]
        body = {"messages": judge_prompt(text, ctx), "max_tokens": 2, "temperature": 0, "stream": False,
                "chat_template_kwargs": {"enable_thinking": False}, "grammar": "root ::= [A-H]"}
        if not self.cfg.get("live_judge_url"):
            body["id_slot"] = self.cfg.get("live_judge_slot", 1)  # не сбивать кэш разговора (ячейка 0)
        t0 = time.time()
        async with session.post(self.url + "/v1/chat/completions", json=body,
                                headers={"Authorization": "Bearer " + self.key} if self.key else {},
                                timeout=_timeout(self.cfg.get("live_judge_timeout_s", 0.8))) as r:
            data = await r.json(content_type=None)
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        kind = parse_judge(msg.get("content") or "")
        if kind:
            self.cache[k] = (kind, round((time.time() - t0) * 1000))
            if len(self.cache) > 500:
                self.cache.pop(next(iter(self.cache)))
        return self.cache.get(k)


def _timeout(s):
    import aiohttp
    return aiohttp.ClientTimeout(total=s)


def fallback(text: str, ctx: Context) -> Decision:
    """Прежний словарь: если судьи нет или он не успел."""
    q = quick(text, ctx)
    words = norm(text).split()
    if ctx.state != "idle" and len(words) <= 3 and all(w in REACT_W for w in words):
        return Decision("continue", True, "fallback", "словарь реакций")
    return Decision(q.kind, True, "fallback", q.why)


async def decide(text: str, ctx: Context, judge: Judge = None, session=None) -> Decision:
    t0 = time.time()
    d = quick(text, ctx)
    if not d.sure and judge is not None and judge.enabled and session is not None:
        try:
            res = await asyncio.wait_for(judge.ask(session, text, ctx), judge.cfg.get("live_judge_timeout_s", 0.8))
            if res:
                d = Decision(res[0], True, "judge", f"судья за {res[1]} мс")
        except Exception as e:  # не успел или недоступен — словарь
            log.info("судья реплики не ответил: %r", e)
    if not d.sure:
        d = fallback(text, ctx)
    d.ms = round((time.time() - t0) * 1000, 1)
    return d


DECISIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "live_decisions.jsonl")


def log_decision(text: str, ctx: Context, d: Decision, acted: str = ""):
    """Журнал решений (только на этом компьютере) — материал для настройки и будущего обучения."""
    try:
        os.makedirs(os.path.dirname(DECISIONS_FILE), exist_ok=True)
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text, "state": ctx.state,
                                "said": ctx.said[-120:], "asked": ctx.asked, "story": ctx.story,
                                "partial": ctx.partial, "kind": d.kind, "source": d.source, "why": d.why,
                                "ms": d.ms, "acted": acted}, ensure_ascii=False) + "\n")
    except OSError:
        pass
