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
        "description": ("Погода: сейчас и прогноз на сегодня/завтра/послезавтра. city — город; если Александр не назвал, "
                        "не указывай — возьмётся из памяти (если города в памяти нет, спроси его и запомни)."),
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string"}, "day": {"type": "string", "enum": ["now", "today", "tomorrow", "after_tomorrow"]}}}}},
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
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        os.replace(FILE, FILE + dt.datetime.now().strftime(".bad-%Y%m%d-%H%M%S"))
        return []


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


async def _weather(city, day, session):
    """Город → координаты (open-meteo geocoding) → прогноз (api.met.no; оба доступны из сети Александра)."""
    geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": city, "count": 1, "language": "ru"})
    async with session.get(geo_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        g = await r.json(content_type=None)
    res = (g.get("results") or [None])[0]
    if not res:
        return {"ok": False, "error": f"не нашла город «{city}»"}
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
    if day in ("today", "tomorrow", "after_tomorrow"):
        target = (dt.datetime.now(dt.timezone.utc).astimezone().date() +
                  dt.timedelta(days={"today": 0, "tomorrow": 1, "after_tomorrow": 2}[day]))
        temps, rain, skies = [], 0.0, []
        for t in ts:
            when = dt.datetime.fromisoformat(t["time"].replace("Z", "+00:00")).astimezone()
            if when.date() != target:
                continue
            temps.append(t["data"]["instant"]["details"].get("air_temperature"))
            n6 = t["data"].get("next_6_hours") or t["data"].get("next_1_hours") or {}
            rain += float((n6.get("details") or {}).get("precipitation_amount") or 0) if "next_6_hours" in t["data"] and when.hour % 6 == 0 else 0
            if 9 <= when.hour <= 18 and n6.get("summary"):
                skies.append(_symbol(n6["summary"].get("symbol_code")))
        temps = [x for x in temps if x is not None]
        if temps:
            out["forecast"] = {"date": target.isoformat(), "min": round(min(temps)), "max": round(max(temps)),
                               "precipitation_mm": round(rain, 1),
                               "day_sky": max(set(skies), key=skies.count) if skies else None}
    return out


def _city_from_memory():
    for f in memory._load():
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
            t = dt.datetime.now() + dt.timedelta(minutes=float(args["in_minutes"]))
        elif args.get("at"):
            t = _parse_at(args["at"])
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
        items = _load()
        keep = [] if q.strip() in ("все", "всё", "all") else [r for r in items if not all(w in r["text"].lower() for w in q.split())]
        removed = len(items) - len(keep)
        _save(keep)
        return {"ok": bool(removed), "cancelled": removed, **({} if removed else {"error": "такого напоминания нет"})}
    return {"ok": False, "error": f"неизвестный инструмент {name}"}
