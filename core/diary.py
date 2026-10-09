"""Дневник разговоров: Ксения помнит не только факты, но и о чём говорили («вчера ты рассказывал про поездку —
как съездил?»). Совет Fable (REVIEW-3, раздел 10, п. 4).

Когда разговор затих (diary_idle_s, 10 мин), мозг во второй ячейке (не сбивая кэш разговора) коротко записывает,
о чём говорили и что стоит спросить потом. Последние записи попадают в системную подсказку — вместе с фактами памяти
и тогда же, когда она обновляется (пересчёт кэша мозга — только в паузе). Хранится только на этом компьютере
(data/diary.json, не в git).
"""
import datetime as dt
import json
import os
import re

import aiohttp

ROOT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.normpath(os.path.join(ROOT, "..", "data", "diary.json"))
KEEP = 40
changed = {"flag": False}
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")

PROMPT = (
    "Ниже — разговор Ксении (голосовой помощницы) с Александром. Запиши для её дневника очень коротко:\n"
    "summary — о чём говорили и что важного Александр рассказал (планы, дела, настроение), 1–2 предложения, "
    "без технических подробностей и без проверок работы самой Ксении;\n"
    "followup — о чём уместно спросить его в следующий раз («как прошла поездка?») или пустая строка.\n"
    "Если разговор пустой или только служебный — summary пустая строка.\n"
    "Ответь только JSON: {\"summary\": \"…\", \"followup\": \"…\"}"
)


def load():
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("entries"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"entries": [], "upto": 0}


def save(d):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def _when(date_s, today=None):
    today = today or dt.date.today()
    try:
        d = dt.date.fromisoformat(date_s)
    except ValueError:
        return date_s
    if d == today:
        return "сегодня"
    if d == today - dt.timedelta(days=1):
        return "вчера"
    return f"{d.day} {MONTHS[d.month - 1]}"


def prompt_block(n=5, today=None):
    entries = [e for e in load()["entries"] if e.get("summary")][-n:]
    if not entries:
        return ""
    lines = ["", "Недавние разговоры (вспоминай изредка и к месту, как друг; не пересказывай подряд):"]
    for e in entries:
        line = f"- {_when(e.get('date', ''), today)}: {e['summary']}"
        if e.get("followup"):
            line += f" (можно спросить: {e['followup']})"
        lines.append(line)
    return "\n".join(lines) + "\n"


def dialogue_text(history, start, limit=6000):
    """Реплики Александра и Ксении без служебных пометок и инструментов."""
    out = []
    for m in history[start:]:
        c = m.get("content")
        if not isinstance(c, str) or not c.strip():
            continue
        if m.get("role") == "user":
            if c.startswith("(служебно"):
                continue
            out.append("Александр: " + c.split("\n\n(служебно", 1)[0].strip())
        elif m.get("role") == "assistant":
            out.append("Ксения: " + re.sub(r"\[\w+\]\s*", "", c).strip())
    text = "\n".join(out)
    return text[-limit:]


def user_turns(history, start):
    return sum(1 for m in history[start:] if m.get("role") == "user" and isinstance(m.get("content"), str)
               and not m["content"].startswith("(служебно"))


def parse(answer: str):
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    s = str(d.get("summary") or "").strip()
    f = str(d.get("followup") or "").strip()
    return {"summary": s[:300], "followup": f[:150]}


async def summarize(session, history, brain_url, key, slot=1, min_turns=3):
    """Записать в дневник новый кусок разговора (если в нём хотя бы min_turns реплик Александра)."""
    d = load()
    upto = d.get("upto", 0)
    if upto > len(history):  # историю очистили — начать с текущего конца
        d["upto"] = len(history)
        save(d)
        return None
    if user_turns(history, upto) < min_turns:
        return None
    text = dialogue_text(history, upto)
    body = {"messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
            "max_tokens": 250, "temperature": 0.3, "stream": False, "id_slot": slot,
            "chat_template_kwargs": {"enable_thinking": False}}
    async with session.post(brain_url + "/v1/chat/completions", json=body,
                            headers={"Authorization": "Bearer " + key} if key else {},
                            timeout=aiohttp.ClientTimeout(total=90)) as r:
        data = await r.json(content_type=None)
    answer = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    entry = parse(answer)
    d["upto"] = len(history)
    if entry and entry["summary"]:
        d["entries"] = (d["entries"] + [{"date": dt.date.today().isoformat(), **entry}])[-KEEP:]
        changed["flag"] = True
    save(d)
    return entry
