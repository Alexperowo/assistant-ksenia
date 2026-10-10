"""Центр управления Ксенией — для человека, а не программиста (решение Александра 2026-10-09).

Всё, что раньше настраивалось в коде и config.json, — здесь, понятными словами: переключатели, режимы,
память, правила, напоминания, дневник, состояние частей и перезапуск. Один и тот же центр открывается
в приложении на планшете/телефоне и на компьютере (через шлюз pwa/).

Настройки человека хранятся отдельно от кода — data/settings.json (не в git, обновления их не сотрут)
и накладываются поверх core/config.json при запуске ядра.
"""
import asyncio
import glob
import json
import os
import subprocess
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.normpath(os.path.join(ROOT, "..", "data", "settings.json"))
REPORTS = os.path.normpath(os.path.join(ROOT, "..", "logs", "reports"))

# Переключатели центра: ключ настроек ядра -> как показать человеку. Только то, что понятно без кода.
SETTINGS = [
    {"key": "live_mode", "group": "talk", "type": "bool", "default": True,
     "label": "Живой разговор", "hint": "В режиме разговора наушников слушаю всегда, можно перебивать"},
    {"key": "voice_output", "group": "talk", "type": "choice", "default": "auto",
     "label": "Голос Ксении", "hint": "Микрофон всё равно в наушниках",
     "options": [["auto", "В наушники, если подключены"], ["monitor", "В колонки монитора"]]},
    {"key": "live_backchannels", "group": "talk", "type": "bool", "default": False,
     "label": "Поддакивать «угу»", "hint": "Когда ты долго рассказываешь и делаешь паузу"},
    {"key": "filler_s", "group": "talk", "type": "choice", "default": 2.0,
     "label": "«Хм, секунду», если думаю долго",
     "options": [[0, "Не говорить"], [2.0, "Через 2 секунды"], [3.0, "Через 3 секунды"]]},
    {"key": "night_gain", "group": "night", "type": "choice", "default": 0.75,
     "label": "Ночью говорить", "options": [[1.0, "Как днём"], [0.75, "Чуть тише"], [0.5, "Тихо"]]},
    {"key": "night_from", "group": "night", "type": "choice", "default": 23,
     "label": "Ночь начинается", "options": [[21, "в 21:00"], [22, "в 22:00"], [23, "в 23:00"], [0, "в 0:00"]]},
    {"key": "night_to", "group": "night", "type": "choice", "default": 7,
     "label": "Ночь кончается", "options": [[6, "в 6:00"], [7, "в 7:00"], [8, "в 8:00"], [9, "в 9:00"]]},
    {"key": "reminders_both", "group": "messages", "type": "bool", "default": True,
     "label": "Напоминания и в колонки", "hint": "Если наушники надеты, повторю напоминание и в колонки монитора"},
    {"key": "watch_poll_s", "group": "messages", "type": "choice", "default": 90,
     "label": "Проверять важные сообщения",
     "options": [[60, "Каждую минуту"], [90, "Каждые полторы минуты"], [180, "Каждые 3 минуты"]]},
    {"key": "watch_remind_s", "group": "messages", "type": "choice", "default": 600,
     "label": "Напоминать о непрочитанном",
     "options": [[300, "Через 5 минут"], [600, "Через 10 минут"], [1200, "Через 20 минут"]]},
    {"key": "diary_enabled", "group": "memory", "type": "bool", "default": True,
     "label": "Вести дневник разговоров", "hint": "Помню, о чём мы говорили, и иногда спрашиваю, как дела"},
]
KEYS = {s["key"]: s for s in SETTINGS}
SERVICES = [("ksenia-brain", "Мозг"), ("ksenia-voice-out", "Голос"), ("ksenia-voice-in", "Слух"),
            ("ksenia-judge", "Судья перебиваний"), ("ksenia-core", "Ядро"), ("ksenia-unblock", "Обход для YouTube"),
            ("ksenia-pwa", "Связь с планшетом")]
SERVICE_NAMES = {s for s, _ in SERVICES}


def load_user_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if k in KEYS} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def apply_user_settings(config: dict):
    """При запуске ядра: настройки человека поверх config.json."""
    config.update(load_user_settings())


def _save_user_settings(d):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_FILE)


def coerce(spec, value):
    """Значение из браузера -> тип настройки; чужое значение -> ValueError."""
    if spec["type"] == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("нужно да/нет")
    allowed = [o[0] for o in spec["options"]]
    for a in allowed:
        if value == a or (isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == float(a)):
            return a
    raise ValueError("такого варианта нет")


def set_setting(config: dict, key, value):
    spec = KEYS.get(key)
    if not spec:
        raise ValueError("нет такой настройки")
    v = coerce(spec, value)
    config[key] = v
    d = load_user_settings()
    d[key] = v
    _save_user_settings(d)
    return v


def settings_view(config):
    out = []
    for s in SETTINGS:
        item = {k: s[k] for k in ("key", "group", "type", "label") if k in s}
        item["value"] = config.get(s["key"], s["default"])
        if "hint" in s:
            item["hint"] = s["hint"]
        if "options" in s:
            item["options"] = s["options"]
        out.append(item)
    return out


async def _run(*argv, timeout=10):
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, out.decode("utf-8", "replace").strip()
    except Exception as e:
        return -1, repr(e)


async def services_view():
    res = []
    for unit, label in SERVICES:
        rc, st = await _run("systemctl", "--user", "is-active", unit)
        res.append({"unit": unit, "label": label, "state": st,
                    "ok": st == "active", "word": {"active": "работает", "activating": "запускается",
                                                   "inactive": "выключено", "failed": "сбой"}.get(st, st)})
    return res


async def restart_service(unit):
    """Перезапуск части Ксении. Ядро и шлюз перезапускаются с задержкой — чтобы ответ успел уйти."""
    if unit not in SERVICE_NAMES:
        raise ValueError("нет такой части")
    if unit in ("ksenia-core", "ksenia-pwa"):
        rc, out = await _run("systemd-run", "--user", "--on-active=2", "--collect", "systemctl", "--user", "restart", unit)
    else:
        rc, out = await _run("systemctl", "--user", "restart", unit, timeout=60)
    return rc == 0


def latest_report():
    files = sorted(glob.glob(os.path.join(REPORTS, "week-*.md")))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return {"file": os.path.basename(files[-1]), "text": f.read()[:6000]}


def notes_view(path, n=15):
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.rstrip() for ln in f if ln.startswith("- ")]
    except OSError:
        return []
    out = []
    for ln in lines[-n:][::-1]:
        when, _, text = ln[2:].partition(" — ")
        out.append({"when": when[5:16], "text": text.split("  \\n")[0].strip()})
    return out


def reminders_view(items):
    out = []
    for r in sorted(items, key=lambda x: x.get("ts", 0)):
        t = time.localtime(r["ts"])
        day = "сегодня" if t.tm_yday == time.localtime().tm_yday else time.strftime("%d.%m", t)
        out.append({"id": r.get("id"), "text": r.get("text"), "when": f"{day} в {time.strftime('%H:%M', t)}"})
    return out


def run_sync(argv, timeout=5):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
