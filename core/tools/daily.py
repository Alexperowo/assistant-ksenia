"""Повседневное: погода (wttr.in — доступен из сети Александра) и напоминания/таймеры.

Напоминания хранятся в data/reminders.json и переживают перезапуск. Когда наступает время, ядро
(reminders_loop) говорит напоминание голосом (приглушая музыку) и показывает уведомление на экране.
Город для погоды: из аргумента или из памяти (факт вида «живёт в городе …»).
"""
import asyncio
import datetime as dt
import json
import os
import secrets
import subprocess
import urllib.parse

import aiohttp

from tools import memory

ROOT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.normpath(os.path.join(ROOT, "..", "..", "data", "reminders.json"))

SCHEMAS = [
    {"type": "function", "function": {
        "name": "weather",
        "description": ("Погода: сейчас и прогноз на сегодня/завтра/послезавтра или на неделю (week). city — город; если Александр не назвал, "
                        "не указывай — возьмётся из памяти (если города в памяти нет, спроси его и запомни)."),
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string"}, "day": {"type": "string", "enum": ["now", "today", "tomorrow", "after_tomorrow", "week"]}}}}},
    {"type": "function", "function": {
        "name": "remind_set",
        "description": ("Поставить напоминание или таймер. text — что напомнить. Время: in_minutes (через сколько минут) "
                        "ИЛИ at в формате «ЧЧ:ММ» или «ГГГГ-ММ-ДД ЧЧ:ММ» (текущее время есть в служебной пометке)."),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "in_minutes": {"type": "number"}, "at": {"type": "string"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "remind_list",
        "description": "Какие напоминания стоят.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "remind_cancel",
        "description": "Отменить напоминание: query — слова из его текста (или «все»).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]
TIMEOUTS = {"weather": 30}


def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        os.replace(FILE, FILE + dt.datetime.now().strftime(".bad-%Y%m%d-%H%M%S"))
        return []
    # одна кривая запись (правка руками) раньше роняла due() каждые 5 секунд — и не срабатывало ни одно напоминание
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and isinstance(r.get("ts"), (int, float))
            and isinstance(r.get("text"), str)]


def _save(items):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def _parse_at(s):
    s = s.strip()
    now = dt.datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%H:%M", "%H.%M"):
        try:
            t = dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt in ("%H:%M", "%H.%M"):
            t = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if t <= now:
                t += dt.timedelta(days=1)  # «в 7:00», а уже позже — значит завтра
        elif t <= now:
            return "past"  # полная дата в прошлом: раньше такое напоминание срабатывало сразу же
        return t
    return None


def _say_when(t):
    now = dt.datetime.now()
    day = "сегодня" if t.date() == now.date() else ("завтра" if t.date() == (now + dt.timedelta(days=1)).date()
                                                    else t.strftime("%d.%m"))
    return f"{day} в {t.strftime('%H:%M')}"


def due():
    """Напоминания, время которых пришло (удаляются из списка)."""
    items = _load()
    now = dt.datetime.now().timestamp()
    ready = [r for r in items if r["ts"] <= now]
    if ready:
        _save([r for r in items if r["ts"] > now])
    return ready


def notify(text):
    try:
        subprocess.Popen(["notify-send", "-a", "Ксения", "-i", "appointment-soon", "-u", "critical",
                          "Напоминание", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


UA = {"User-Agent": "Ksenia/0.1 github.com/Alexperowo/assistant-ksenia"}
SYMBOLS = {"clearsky": "ясно", "fair": "малооблачно", "partlycloudy": "переменная облачность", "cloudy": "пасмурно",
           "fog": "туман", "lightrain": "небольшой дождь", "rain": "дождь", "heavyrain": "сильный дождь",
           "lightrainshowers": "небольшой ливень", "rainshowers": "ливень", "heavyrainshowers": "сильный ливень",
           "lightsleet": "мокрый снег", "sleet": "мокрый снег", "heavysleet": "сильный мокрый снег",
           "lightsnow": "небольшой снег", "snow": "снег", "heavysnow": "сильный снег",
           "lightsnowshowers": "снегопад", "snowshowers": "снегопад", "heavysnowshowers": "сильный снегопад",
           "rainandthunder": "дождь с грозой", "heavyrainandthunder": "ливень с грозой", "lightrainandthunder": "гроза"}


def _symbol(code):
    base = (code or "").split("_")[0]
    return SYMBOLS.get(base, base)


def _nominative_guesses(city):
    """«в Москве», «в Гусь-Хрустальном» (так город лежит в памяти) -> «Москва», «Гусь-Хрустальный»."""
    out = []
    for end, rep in (("ском", "ск"), ("ом", "ый"), ("ем", "ий"), ("е", "а"), ("и", "ь"), ("е", "")):
        if city.endswith(end) and len(city) > len(end) + 2:
            out.append(city[: -len(end)] + rep)
    return out


async def _weather(city, day, session):
    """Город → координаты (open-meteo geocoding) → прогноз (api.met.no; оба доступны из сети Александра)."""
    res = None
    for name in [city] + _nominative_guesses(city):
        geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": name, "count": 1, "language": "ru"})
        async with session.get(geo_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            g = await r.json(content_type=None)
        res = (g.get("results") or [None])[0]
        if res:
            break
    if not res:
        return {"ok": False, "error": f"не нашла город «{city}»",
                "note": "название могло быть искажено распознаванием речи: подумай, какой настоящий город похож "
                        "по звучанию, и сразу вызови weather с ним; переспроси, только если вариантов нет"}
    lat, lon = round(res["latitude"], 3), round(res["longitude"], 3)
    url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"
    async with session.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status != 200:
            return {"ok": False, "error": f"погода не ответила ({r.status})"}
        d = await r.json(content_type=None)
    ts = d["properties"]["timeseries"]
    now = ts[0]
    det = now["data"]["instant"]["details"]
    nxt = now["data"].get("next_1_hours") or now["data"].get("next_6_hours") or {}
    out = {"ok": True, "city": res.get("name"), "region": res.get("admin1"),
           "now": {"temp": round(det.get("air_temperature", 0)), "wind_ms": det.get("wind_speed"),
                   "humidity": det.get("relative_humidity"), "sky": _symbol((nxt.get("summary") or {}).get("symbol_code"))}}
    steps = [(dt.datetime.fromisoformat(t["time"].replace("Z", "+00:00")).astimezone(), t) for t in ts]
    today = dt.datetime.now(dt.timezone.utc).astimezone().date()
    if day in ("today", "tomorrow", "after_tomorrow"):
        f = _day_summary(steps, today + dt.timedelta(days={"today": 0, "tomorrow": 1, "after_tomorrow": 2}[day]))
        if f:
            out["forecast"] = f
    elif day == "week":
        # раньше было только до послезавтра, и Ксения называла три дня «неделей» (живой тест 2026-10-08)
        days = [f for k in range(7) if (f := _day_summary(steps, today + dt.timedelta(days=k)))]
        out["week"] = days
        out["note"] = f"прогноз на {len(days)} дн.; назови дни недели и главное, коротко"
    return out


WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def _day_summary(steps, target):
    temps, rain, skies = [], 0.0, []
    for k, (when, t) in enumerate(steps):
        if when.date() != target:
            continue
        temps.append(t["data"]["instant"]["details"].get("air_temperature"))
        # осадки: в ближайшие ~2,5 суток шаги часовые (берём next_1_hours), дальше — по 6 часов в 0/6/12/18 UTC
        # (next_6_hours). Раньше брались только шаги с местным часом, кратным 6: в UTC+3 шестичасовых шагов
        # с таким часом нет, и на послезавтра осадки всегда выходили 0
        gap = (steps[k + 1][0] - when).total_seconds() / 3600 if k + 1 < len(steps) else None
        one, six = t["data"].get("next_1_hours"), t["data"].get("next_6_hours")
        block = one if one and (gap == 1 or (gap is None and not six)) else (six or one or {})
        rain += float((block.get("details") or {}).get("precipitation_amount") or 0)
        n6 = six or one or {}
        if 9 <= when.hour <= 18 and n6.get("summary"):
            skies.append(_symbol(n6["summary"].get("symbol_code")))
    temps = [x for x in temps if x is not None]
    if not temps:
        return None
    return {"date": target.isoformat(), "weekday": WEEKDAYS[target.weekday()], "min": round(min(temps)),
            "max": round(max(temps)), "precipitation_mm": round(rain, 1),
            "day_sky": max(set(skies), key=skies.count) if skies else None}


def _city_from_memory():
    # последний записанный город — самый свежий (раньше брался первый: «живёт в Москве» из старого теста
    # перебивал позже записанный настоящий город)
    for f in reversed(memory._load()):
        fact = f["fact"].lower()
        for key in ("живёт в ", "живет в ", "город ", "из города "):
            if key in fact:
                return f["fact"][fact.index(key) + len(key):].strip(" .").split(",")[0]
    return None


async def call(name, args, session):
    if name == "weather":
        city = (args.get("city") or "").strip() or _city_from_memory()
        if not city:
            return {"ok": False, "error": "не знаю город — спроси Александра, где он, и запомни (memory_remember)"}
        return await _weather(city, args.get("day") or "now", session)
    if name == "remind_set":
        text = " ".join((args.get("text") or "").split())
        if not text:
            return {"ok": False, "error": "не сказано, о чём напомнить"}
        if args.get("in_minutes"):
            try:
                minutes = float(args["in_minutes"])
            except (TypeError, ValueError):
                minutes = -1
            if not 0 < minutes <= 366 * 24 * 60:  # отрицательное — сработало бы сразу, огромное — OverflowError
                return {"ok": False, "error": f"не поняла, через сколько минут: «{args['in_minutes']}»"}
            t = dt.datetime.now() + dt.timedelta(minutes=minutes)
        elif args.get("at"):
            t = _parse_at(str(args["at"]))
            if t == "past":
                return {"ok": False, "error": f"время «{args['at']}» уже прошло"}
            if not t:
                return {"ok": False, "error": f"не поняла время «{args['at']}»"}
        else:
            return {"ok": False, "error": "не сказано когда"}
        items = _load()
        items.append({"id": secrets.token_hex(3), "text": text, "ts": t.timestamp(), "when": t.isoformat(timespec="minutes")})
        items.sort(key=lambda r: r["ts"])
        _save(items)
        return {"ok": True, "text": text, "when": _say_when(t)}
    if name == "remind_list":
        items = _load()
        return {"ok": True, "reminders": [{"text": r["text"], "when": _say_when(dt.datetime.fromtimestamp(r["ts"]))}
                                          for r in items]}
    if name == "remind_cancel":
        q = (args.get("query") or "").lower()
        if not q.split():
            # пустой запрос: all() по пустому списку — истина, и стирались ВСЕ напоминания
            return {"ok": False, "error": "не сказано, какое напоминание отменить"}
        items = _load()
        keep = [] if q.strip() in ("все", "всё", "all") else [r for r in items if not all(w in r["text"].lower() for w in q.split())]
        removed = len(items) - len(keep)
        _save(keep)
        return {"ok": bool(removed), "cancelled": removed, **({} if removed else {"error": "такого напоминания нет"})}
    return {"ok": False, "error": f"неизвестный инструмент {name}"}
