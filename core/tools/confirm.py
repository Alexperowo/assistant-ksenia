"""Общее подтверждение рискованных действий (отправка сообщения, «Оплатить», «Удалить» на сайте…).

Инструмент не выполняет действие сам: он регистрирует его здесь (prepare) и просит модель спросить
Александра. Выполняет ЯДРО, если следующая реплика Александра — ясное «да» (см. core.is_affirmative).
Одно ожидающее действие за раз, живёт 3 минуты.
"""
import secrets
import time

_pending = {}


def prepare(label: str, run, ttl: float = 180) -> str:
    """run — функция без аргументов, возвращающая корутину с результатом-словарём."""
    cid = secrets.token_hex(4)
    _pending.clear()
    _pending["item"] = {"id": cid, "label": label, "run": run, "expires": time.time() + ttl}
    return cid


def current():
    p = _pending.get("item")
    if p and time.time() > p["expires"]:
        _pending.clear()
        return {"expired": True, "label": p["label"]}
    return p


def take():
    p = _pending.pop("item", None)
    return p if p and time.time() <= p["expires"] else None


def cancel():
    _pending.clear()
