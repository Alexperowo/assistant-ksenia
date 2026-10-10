"""Режим голоса и отпечаток Александра.

Режимы (переключает только сам Александр — его голосом):
  guest       — чужой голос: Ксения разговаривает вежливо, но не выполняет действий и не принимает «да»;
  owner_only  — чужой голос Ксения игнорирует (не отвечает).
Запись образца: несколько обычных реплик Александра подряд (их голос усредняется в отпечаток в voice-in).
Проверку «кто говорит» и запрет действий для гостя делает ЯДРО, не модель.
"""
import json
import os

import aiohttp

from tools import confirm

ROOT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.normpath(os.path.join(ROOT, "..", "..", "data", "voice_policy.json"))
VOICE_IN = "http://127.0.0.1:18120"
ENROLL_PHRASES = 5


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f).get("mode", "guest")
    except Exception:
        return "guest"


STATE = {"mode": _load(), "enrolling": 0}

SCHEMAS = [
    {"type": "function", "function": {
        "name": "voice_mode",
        "description": ("Режим чужого голоса: guest — с гостями разговаривать, но действий не выполнять; "
                        "owner_only — слушать только Александра, чужие голоса игнорировать. "
                        "Вызывай, когда Александр просит «гостевой режим» / «слушай только меня»."),
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["guest", "owner_only"]}}, "required": ["mode"]}}},
    {"type": "function", "function": {
        "name": "voice_enroll",
        "description": ("Режим голоса и отпечаток. status — какой сейчас режим и записан ли отпечаток (вызывай, когда спрашивают про режим); start — начать запись образца (дальше он просто говорит "
                        f"{ENROLL_PHRASES} обычных фраз; задавай ему вопросы, чтобы он рассказывал), status — "
                        "есть ли отпечаток, clear — удалить отпечаток."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "status", "clear"]}}, "required": ["action"]}}},
]


def _save_mode(mode):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump({"mode": mode}, f)


async def _vp(session, action):
    async with session.post(f"{VOICE_IN}/voiceprint/{action}", timeout=aiohttp.ClientTimeout(total=10)) as r:
        return await r.json(content_type=None)


async def call(name, args, session):
    if name == "voice_mode":
        mode = args.get("mode")
        if mode not in ("guest", "owner_only"):
            return {"ok": False, "error": "неизвестный режим"}
        if mode == "guest" and STATE["mode"] == "owner_only" and not args.get("_confirmed"):
            # ослабить защиту — только после «да»: иначе чужой текст (страница, сообщение) снимал бы её молча
            return confirm.ask("включить гостевой режим", lambda: call(name, {**args, "_confirmed": True}, session),
                               question="Включить гостевой режим? Чужие голоса я снова буду слышать.")
        STATE["mode"] = mode
        _save_mode(mode)
        st = await _vp(session, "status")
        res = {"ok": True, "mode": mode}
        if not st.get("enrolled"):
            res["note"] = "отпечатка голоса ещё нет — режим заработает после записи образца (voice_enroll start)"
        return res
    if name == "voice_enroll":
        a = args.get("action", "status")
        if a == "start":
            try:
                await _vp(session, "begin")
            except Exception:
                pass
            STATE["enrolling"] = ENROLL_PHRASES
            return {"ok": True, "started": True, "phrases": ENROLL_PHRASES,
                    "note": f"попроси Александра рассказать о чём-нибудь — следующие {ENROLL_PHRASES} его фраз станут образцом"}
        if a == "clear":
            async def clear():
                STATE["enrolling"] = 0
                return await _vp(session, "clear")
            # без образца «да» и «стоп» принимаются от любого голоса — только после «да» (аудит Fable, B11)
            return confirm.ask("стереть образец голоса Александра", clear,
                               question="Стереть образец твоего голоса? Пока не запишем новый, я не отличу тебя от гостей.")
        st = await _vp(session, "status")
        meaning = {"guest": "гостевой: с ЧУЖИМИ голосами Ксения говорит, но действий для них не выполняет; Александру — всё как обычно",
                   "owner_only": "только Александр: чужие голоса Ксения не слушает"}[STATE["mode"]]
        return {"ok": True, "enrolled": st.get("enrolled"), "mode": STATE["mode"], "meaning": meaning,
                "enrolling_left": STATE["enrolling"],
                **({} if st.get("enrolled") else {"note": "без отпечатка Ксения не отличает голоса — режимы начнут работать после записи образца"})}
    return {"ok": False, "error": f"неизвестный инструмент {name}"}
