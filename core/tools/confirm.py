"""Общее подтверждение рискованных действий (отправка сообщения, «Оплатить», «Удалить» на сайте…).

Инструмент не выполняет действие сам: он регистрирует его здесь (ask/prepare) и сразу возвращает вопрос,
который ЯДРО произносит дословно (speak_verbatim) — Александр слышит ровно то, что будет сделано, а не пересказ
модели. Выполняет ЯДРО, если следующая реплика Александра — ясное «да» (см. core.is_affirmative).
Одно ожидающее действие за раз, живёт 3 минуты.

CONTEXT — что сказал Александр в текущей реплике (ядро выставляет перед ходом): инструменты, которым нужно
его явное слово (например, «запомни»), смотрят сюда, а не верят модели.
"""
import secrets
import time

_pending = {}
_listeners = []
CONTEXT = {"user_text": "", "internal": False, "affirmative": False}


def on_change(fn):
    """fn(event: dict) — например, показать кнопки «Да»/«Нет» на планшете."""
    _listeners.append(fn)


def _notify(event):
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception:
            pass


def prepare(label: str, run, ttl: float = 180, question: str = None) -> str:
    """run — функция без аргументов, возвращающая корутину с результатом-словарём."""
    cid = secrets.token_hex(4)
    _pending.clear()
    question = question or f"{label[:1].upper()}{label[1:]}?"
    _pending["item"] = {"id": cid, "label": label, "run": run, "expires": time.time() + ttl, "question": question}
    _notify({"type": "confirm", "id": cid, "label": label, "question": question})
    return cid


def ask(label: str, run, question: str, **extra) -> dict:
    """Зарегистрировать действие и вернуть результат инструмента с вопросом, который ядро скажет дословно."""
    cid = prepare(label, run, question=question)
    return {"ok": True, "prepared": True, "confirm_id": cid, "speak_verbatim": question, **extra,
            "note": ("НЕ выполнено. Вопрос уже прозвучал дословно — не повторяй его и не пересказывай, просто жди "
                     "ответа Александра. Выполнит ядро после его «да».")}


def current():
    p = _pending.get("item")
    if p and time.time() > p["expires"]:
        _pending.clear()
        _notify({"type": "confirm_clear", "id": p["id"], "reason": "expired"})
        return {"expired": True, "label": p["label"]}
    return p


def take():
    p = _pending.pop("item", None)
    if p:
        _notify({"type": "confirm_clear", "id": p["id"], "reason": "taken"})
    return p if p and time.time() <= p["expires"] else None


def cancel():
    p = _pending.pop("item", None)
    if p:
        _notify({"type": "confirm_clear", "id": p["id"], "reason": "cancelled"})
