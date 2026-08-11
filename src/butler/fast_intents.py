from __future__ import annotations

import re
from datetime import datetime


MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_PREFIX = r"(?:(?:ксения\s+)?(?:скажи|назови|покажи|подскажи)\s+)?"
_DATE = (
    rf"{_PREFIX}(?:какое\s+сегодня\s+число|какая\s+сегодня\s+дата|"
    r"какой\s+сегодня\s+день(?:\s+недели)?|текущая\s+дата|"
    r"сегодняшняя\s+дата|текущую\s+дату|сегодняшнюю\s+дату)"
)
_TIME = (
    rf"{_PREFIX}(?:который\s+(?:сейчас\s+)?час|сколько\s+(?:сейчас\s+)?времени|"
    r"текущее\s+время|текущего\s+времени)"
)


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[а-яёa-z0-9]+", text.casefold()))


def _date_answer(now: datetime) -> str:
    return (
        f"Сегодня {WEEKDAYS[now.weekday()]}, {now.day} "
        f"{MONTHS[now.month - 1]} {now.year} года."
    )


def _time_answer(now: datetime) -> str:
    return f"Сейчас {now.hour:02d}:{now.minute:02d}."


def fast_intent_reply(text: str, *, now: datetime | None = None) -> str | None:
    """Answer unambiguous local date/time questions without starting an LLM."""
    normalized = _normalize(text)
    if not normalized or len(normalized) > 120:
        return None
    current = now or datetime.now()
    if re.fullmatch(_DATE, normalized):
        return _date_answer(current)
    if re.fullmatch(_TIME, normalized):
        return _time_answer(current)
    combined = (
        re.fullmatch(rf"{_DATE}\s+(?:и|а\s+также)\s+{_TIME}", normalized)
        or re.fullmatch(rf"{_TIME}\s+(?:и|а\s+также)\s+{_DATE}", normalized)
    )
    if combined:
        return f"{_date_answer(current)} {_time_answer(current)}"
    return None
