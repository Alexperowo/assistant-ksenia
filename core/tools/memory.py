"""Долговременная память Ксении о жизни Александра: только то, что он сказал или подтвердил сам.

Факты хранятся в data/memory.json и попадают в системную подсказку (блок «Что я знаю об Александре»).
Подсказка меняется редко (только при новом факте) — это одна дорогая перезагрузка кэша мозга, не каждая реплика.
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.normpath(os.path.join(ROOT, "..", "..", "data", "memory.json"))
MAX_FACTS = 200

SCHEMAS = [
    {"type": "function", "function": {
        "name": "memory_remember",
        "description": ("Запомнить факт об Александре навсегда (предпочтения, люди, привычки, важные даты). "
                        "Только если он сам попросил запомнить или подтвердил «да, запомни». Коротко, от третьего лица: "
                        "«любит чай с лимоном», «сестру зовут Таня»."),
        "parameters": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}}},
    {"type": "function", "function": {
        "name": "memory_forget",
        "description": "Забыть факт (Александр попросил забыть или факт устарел). query — слова из факта.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "memory_list",
        "description": "Что Ксения помнит об Александре (если он спрашивает «что ты обо мне знаешь?»).",
        "parameters": {"type": "object", "properties": {}}}},
]


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        os.replace(FILE, FILE + time.strftime(".bad-%Y%m%d-%H%M%S"))
        return []


def _save(facts):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(facts[-MAX_FACTS:], f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def prompt_block() -> str:
    facts = _load()
    if not facts:
        return ""
    lines = "\n".join(f"- {f['fact']}" for f in facts)
    return ("\n\nЧто я знаю об Александре (он сам рассказал; используй естественно, к месту, не перечисляй без повода):\n"
            + lines + "\n")


def _norm(s):
    return " ".join((s or "").lower().replace("ё", "е").split())


changed = {"flag": False}  # ядро смотрит: если память изменилась — обновить системную подсказку


async def call(name, args, session):
    facts = _load()
    if name == "memory_remember":
        fact = " ".join((args.get("fact") or "").split())[:300]
        if not fact:
            return {"ok": False, "error": "пустой факт"}
        if any(_norm(f["fact"]) == _norm(fact) for f in facts):
            return {"ok": True, "already": True}
        facts.append({"fact": fact, "added": time.strftime("%Y-%m-%d")})
        _save(facts)
        changed["flag"] = True
        return {"ok": True, "remembered": fact}
    if name == "memory_forget":
        q = _norm(args.get("query"))
        keep = [f for f in facts if not (q and all(w in _norm(f["fact"]) for w in q.split()))]
        removed = len(facts) - len(keep)
        if removed:
            _save(keep)
            changed["flag"] = True
        return {"ok": bool(removed), "forgotten": removed, **({} if removed else {"error": "такого не помню"})}
    if name == "memory_list":
        return {"ok": True, "facts": [f["fact"] for f in facts]}
    return {"ok": False, "error": f"неизвестный инструмент {name}"}
