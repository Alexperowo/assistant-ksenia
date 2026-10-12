"""Краткое содержание выпавшей части разговора: Ксения помнит, с чего начинали, как живой человек.

Окно истории для мозга время от времени прыгает вперёд (полный пересчёт — только в тишине). Раньше старая
половина просто выпадала, и вернуться к первой теме разговора она не могла. Теперь перед прыжком мозг во второй
ячейке (не сбивая кэш разговора) дописывает выпадающее в краткое содержание, и оно идёт в системную подсказку
вместе с памятью и дневником — меняется только вместе с прыжком, когда начало запроса и так пересчитывается.
Схема как у Open WebUI (сжатие старых сообщений + свежий дословный хвост) — анализ Codex 2026-10-12;
решение Александра: «человек помнит, с чего начался разговор».
Дневник — другое: итоги разговоров в одно-два предложения для «как съездил?» через дни.
Хранится только на этом компьютере (data/recap.json, не в git).
"""
import datetime as dt
import json
import os

import aiohttp

import diary

ROOT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.normpath(os.path.join(ROOT, "..", "data", "recap.json"))
LIMIT = 1200  # символов: подсказка не должна расти без конца

PROMPT = (
    "Ты ведёшь краткое содержание долгого разговора голосовой помощницы Ксении с Александром, чтобы она помнила "
    "его, как живой человек. Ниже — прежнее краткое содержание (может быть пустым) и старая часть разговора, "
    "которая выпадает из её дословной памяти. Напиши обновлённое краткое содержание: с чего начинали и о чём "
    "говорили, что решили и сделали, что Ксения обещала, незаконченные темы, имена и подробности, которые называл "
    "Александр, его просьбы о том, как ей себя вести. Свежее — подробнее, давнее — короче. Проверки работы самой "
    "Ксении (который час, самопроверка, тесты связи) не записывай, если в них не было решения. Не цитируй её "
    f"фразы. Обычным текстом по-русски, от третьего лица, не длиннее {LIMIT} символов. Только содержание, без "
    "вступлений."
)


def load() -> dict:
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(text: str):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": text, "updated": dt.datetime.now().isoformat(timespec="seconds")}, f, ensure_ascii=False)
    os.replace(tmp, FILE)


def prompt_block() -> str:
    text = (load().get("text") or "").strip()
    if not text:
        return ""
    return ("\nРаньше в этом разговоре (твоя память о начале разговора — опирайся на неё, если Александр вернётся "
            "к теме; не пересказывай без повода):\n" + text + "\n")


async def update(session, dropped, brain_url, key, slot=1) -> bool:
    """Дописать выпадающие сообщения в краткое содержание. False — мозг не ответил (прежнее остаётся)."""
    talk = diary.dialogue_text(dropped, 0, limit=14000)
    if not talk.strip():
        return False
    old = (load().get("text") or "").strip()
    body = {"messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": f"Прежнее краткое содержание:\n{old or '(пусто)'}\n\n"
                                                     f"Старая часть разговора:\n{talk}"}],
            "max_tokens": 700, "temperature": 0.3, "stream": False, "id_slot": slot,
            "chat_template_kwargs": {"enable_thinking": False}}
    async with session.post(brain_url + "/v1/chat/completions", json=body,
                            headers={"Authorization": "Bearer " + key} if key else {},
                            timeout=aiohttp.ClientTimeout(total=180)) as r:
        data = await r.json(content_type=None)
        status = r.status
    choices = data.get("choices") if isinstance(data, dict) else None
    if status != 200 or not choices:
        return False
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        return False
    if len(text) > LIMIT * 1.5:  # мозг не уложился — обрезать по предложению, а не на полуслове
        cut = text[:int(LIMIT * 1.5)]
        text = cut[:cut.rfind(".") + 1] or cut
    save(text)
    return True
