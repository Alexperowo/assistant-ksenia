"""Общее подтверждение рискованных действий (отправка сообщения, «Оплатить», «Удалить» на сайте…).

Инструмент не выполняет действие сам: он регистрирует его здесь (ask/prepare) и сразу возвращает вопрос,
который ЯДРО произносит дословно (speak_verbatim) — Александр слышит ровно то, что будет сделано, а не пересказ
модели. Выполняет ЯДРО, если следующая реплика Александра — ясное «да» (см. core.is_affirmative).

Одно ожидающее действие за раз, живёт 3 минуты, и только пока вопрос — последнее, что сказала Ксения:
  - TURN — номер хода ядра (любого: ответ Александру, напоминание, находка помощника). Вопрос, после которого
    прозвучало что-то ещё («Пора выпить таблетки. Выпил?»), «да» уже не решает — оно могло быть ответом на другое;
  - вопрос, который не прозвучал (перебили, голос не работал), отменяет ядро в конце хода;
  - второй вопрос в том же ходе не задаётся: одно «да» — одно действие.

CONTEXT — что сказал Александр в текущей реплике (ядро выставляет перед ходом): инструменты, которым нужно
его явное слово (например, «запомни»), смотрят сюда, а не верят модели.
"""
import re
import secrets
import time

# Деньги Ксения не трогает НИКОГДА — ни с «да», ни без (решение Александра). Кнопка с такими словами — отказ
# на любом сайте и в любой программе: «Купить в 1 клик» на странице товара раньше проходила по «да», потому что
# запрет требовал ещё и «платёжного» адреса. «Перевод» — только денежный («Показать перевод» во ВКонтакте можно).
FINANCE = re.compile(r"оплат|купить|покупк|заказ|оформить|в 1 клик|в один клик|списать|спишется|пополнить|"
                     r"перевест\w*\s+(?:[\d\s.,]+)?(?:деньг|руб|₽|на карт)|перевод\w*\s+(?:денег|по номеру|на карт)|"
                     r"\d\s*(?:₽|руб)|"
                     r"донат|пожертв|платн|\bpay|buy|purchase|checkout|order|subscribe|donat|1-click", re.I)


def is_financial(*texts) -> bool:
    return any(t and FINANCE.search(t) for t in texts)


MONEY_REFUSAL = {"ok": False, "error": "оплату, покупки и переводы денег я не делаю — это может только Александр сам"}

_pending = {}
_listeners = []
CONTEXT = {"user_text": "", "internal": False, "affirmative": False, "last_said": ""}
TURN = {"n": 0}


def on_change(fn):
    """fn(event: dict) — например, показать кнопки «Да»/«Нет» на планшете."""
    _listeners.append(fn)


def _notify(event):
    for fn in list(_listeners):
        try:
            fn(event)
        except Exception:
            pass


def prepare(label: str, run, ttl: float = 180, question: str = None, limit: float = 60,
            background: bool = False) -> str:
    """run — функция без аргументов, возвращающая корутину с результатом-словарём.
    limit — сколько секунд ядро даёт действию; background — долгое (установка, обновление): ядро не ждёт его
    в ходе, а скажет о результате, когда закончится."""
    cid = secrets.token_hex(4)
    _pending.clear()
    question = question or f"{label[:1].upper()}{label[1:]}?"
    _pending["item"] = {"id": cid, "label": label, "run": run, "expires": time.time() + ttl, "question": question,
                        "turn": TURN["n"], "limit": limit, "background": background}
    _notify({"type": "confirm", "id": cid, "label": label, "question": question})
    return cid


def ask(label: str, run, question: str, *, limit: float = 60, background: bool = False, **extra) -> dict:
    """Зарегистрировать действие и вернуть результат инструмента с вопросом, который ядро скажет дословно."""
    cur = _pending.get("item")
    if cur and cur["turn"] == TURN["n"] and time.time() < cur["expires"]:
        if cur["question"] == question:
            # модель повторила тот же вызов — вопрос уже прозвучал, второй раз не говорим
            return {"ok": True, "prepared": True, "already_asked": True, "confirm_id": cur["id"], **extra,
                    "note": "этот вопрос уже прозвучал — НЕ вызывай инструмент снова и не повторяй вопрос, просто жди ответа"}
        # другой вопрос в том же ходе: одно «да» решило бы только последний, а прозвучали бы оба
        return {"ok": False, "error": "сначала дождись ответа Александра на прошлый вопрос — одно действие за раз",
                "pending_question": cur["question"]}
    cid = prepare(label, run, question=question, limit=limit, background=background)
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


def peek():
    """Ожидающее действие без побочных эффектов (current() убирает просроченное)."""
    p = _pending.get("item")
    return p if p and time.time() <= p["expires"] else None


def take():
    p = _pending.pop("item", None)
    if p:
        _notify({"type": "confirm_clear", "id": p["id"], "reason": "taken"})
    return p if p and time.time() <= p["expires"] else None


def cancel(reason: str = "cancelled"):
    p = _pending.pop("item", None)
    if p:
        _notify({"type": "confirm_clear", "id": p["id"], "reason": reason})
    return p
