"""Руки на рабочем столе: запуск программ, элементы активного окна, нажать, вписать, прочитать окно.

Основной путь — дерево доступности (AT-SPI, как у экранных дикторов): элементы ищутся по названию
и нажимаются программно, поэтому экранная лупа и разрешение не мешают. Работает через помощник
atspi_helper.py, который запускается системным python3 (в нём есть gi/Atspi).
Рискованные кнопки (удалить, отправить, оплатить…) — только через подтверждение ядром.
"""
import asyncio
import configparser
import glob
import json
import os
import re
import subprocess

from tools import confirm, screen

ROOT = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(ROOT, "atspi_helper.py")
RISKY = re.compile(r"удал|отправ|оплат|купить|стереть|очист|формат|выйти из|сброс|перезагруз|выключ|закрыть без|"
                   r"delete|remove|send|pay|erase|format|reset|shut ?down|reboot", re.I)
APP_DIRS = ["/usr/share/applications", os.path.expanduser("~/.local/share/applications"),
            "/var/lib/flatpak/exports/share/applications", os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
            "/var/lib/snapd/desktop/applications"]

SCHEMAS = [
    {"type": "function", "function": {
        "name": "app_open",
        "description": "Запустить программу по названию («калькулятор», «Firefox», «Dolphin», «настройки», «текстовый редактор»).",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "ui_elements",
        "description": "Активное окно: какие в нём кнопки, пункты меню, вкладки и поля (через доступность, лупа не мешает).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "ui_click",
        "description": "Нажать в активном окне кнопку/пункт меню/вкладку по названию.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "ui_type",
        "description": "Вписать текст в поле активного окна (field — название поля; пусто — поле в фокусе).",
        "parameters": {"type": "object", "properties": {
            "field": {"type": "string"}, "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "window_action",
        "description": "Активное окно: close — закрыть (как Alt+F4), minimize — свернуть, maximize — развернуть/вернуть.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["close", "minimize", "maximize"]}}, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "dictate",
        "description": ("Диктовка: напечатать текст туда, где сейчас курсор (поле, документ, чат). text — дословно, "
                        "как сказал Александр, с нормальной пунктуацией. enter=true — нажать Enter после "
                        "(в чате это отправка — спросит «да»)."),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "enter": {"type": "boolean"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "app_menu",
        "description": ("Открыть или закрыть меню приложений внизу экрана (как «Пуск» в Windows). Чтобы запустить "
                        "конкретную программу, меню не нужно — сразу app_open."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "window_read",
        "description": "Прочитать вслух дословно текст активного окна (через доступность; если её нет — распознаванием).",
        "parameters": {"type": "object", "properties": {}}}},
]

TIMEOUTS = {"dictate": 20, "app_menu": 10, "window_action": 15, "app_open": 20, "ui_elements": 25, "ui_click": 25, "ui_type": 25, "window_read": 60}


HELPER_TIMEOUT_S = 20
PYTHON = "/usr/bin/python3"


async def _helper(cmd, args=None):
    p = await asyncio.create_subprocess_exec(
        PYTHON, HELPER, cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(json.dumps(args or {}, ensure_ascii=False).encode()),
                                          timeout=HELPER_TIMEOUT_S)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return {"ok": False, "error": "дерево доступности не ответило (программа зависла?)"}
    try:
        return json.loads(out.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": f"помощник доступности: {err.decode('utf-8', 'replace')[:200]}"}


def _norm(s):
    return " ".join((s or "").lower().replace("ё", "е").split())


ALIASES = {"настройки": "systemsettings", "параметры": "systemsettings", "параметры системы": "systemsettings",
           "текстовый редактор": "org.kde.kate", "редактор": "org.kde.kate", "блокнот": "org.kde.kate",
           "файлы": "org.kde.dolphin", "проводник": "org.kde.dolphin", "файловый менеджер": "org.kde.dolphin",
           "терминал": "org.kde.konsole", "консоль": "org.kde.konsole", "калькулятор": "org.kde.kcalc",
           "хром": "google-chrome", "гугл хром": "google-chrome", "гугл": "google-chrome",
           "фаерфокс": "firefox_firefox", "файрфокс": "firefox_firefox", "лиса": "firefox_firefox",
           "браузер": None}  # None — браузер по умолчанию


def _desktop_ok(e):
    """Только то, что показывается в KDE (у Александра параллельно стоит Cinnamon для пробы)."""
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "KDE").split(":")
    only = [x for x in e.get("OnlyShowIn", "").split(";") if x]
    notin = [x for x in e.get("NotShowIn", "").split(";") if x]
    return (not only or any(d in only for d in desk)) and not any(d in notin for d in desk)


def _find_app(query):
    q = _norm(query)
    if q in ALIASES:
        app_id = ALIASES[q]
        if app_id is None:
            try:
                app_id = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True,
                                        text=True, timeout=5).stdout.strip().removesuffix(".desktop")
            except Exception:
                app_id = "firefox_firefox"
        for d in APP_DIRS:
            if os.path.exists(os.path.join(d, app_id + ".desktop")):
                return app_id, query

    best, best_score = None, 0
    for d in APP_DIRS:
        for path in glob.glob(os.path.join(d, "*.desktop")):
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(path, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("NoDisplay", "false").lower() == "true" or e.get("Type", "Application") != "Application" \
                    or not _desktop_ok(e):
                continue
            names = [e.get(k, "") for k in ("Name[ru]", "Name", "GenericName[ru]", "GenericName")]
            kw = (e.get("Keywords[ru]", "") + ";" + e.get("Keywords", "")).split(";")
            score = 0
            for nm in names:
                n = _norm(nm)
                if not n:
                    continue
                if n == q:
                    score = max(score, 5)
                elif n.startswith(q) or q in n.split():
                    score = max(score, 4)
                elif q in n:
                    score = max(score, 3)
            if not score and any(_norm(k) == q or (len(q) > 3 and _norm(k).startswith(q[:5])) for k in kw if k):
                score = 2
            if score and e.get("Terminal", "false").lower() == "true":
                score -= 1.5  # консольные программы (vim) — только если ничего другого нет
            if score > best_score:
                best, best_score = (os.path.basename(path)[:-8], e.get("Name[ru]") or e.get("Name")), score
    return best


# коды клавиш Linux для ydotool: Ctrl, Shift, V, Enter
KEY_CTRL, KEY_SHIFT, KEY_V, KEY_ENTER = 29, 42, 47, 28


async def _sh(*argv, input_bytes=None, timeout=5):
    p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(p.communicate(input_bytes), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return -1, b""
    return p.returncode, out


async def _dictate(text: str, enter: bool):
    """Вставка через буфер обмена (русские буквы и знаки ydotool «печатать» не умеет — раскладка) и Ctrl+V;
    в терминале — Ctrl+Shift+V. Прежний буфер возвращается."""
    focused = await _helper("focused")
    window = focused.get("window", "") if isinstance(focused, dict) else ""
    rc, old = await _sh("wl-paste", "--no-newline")
    await _sh("wl-copy", input_bytes=text.encode("utf-8"))
    await asyncio.sleep(0.15)
    terminal = bool(re.search(r"konsole|terminal|терминал|yakuake", window, re.I))
    keys = [f"{KEY_CTRL}:1"] + ([f"{KEY_SHIFT}:1"] if terminal else []) + [f"{KEY_V}:1", f"{KEY_V}:0"] + \
        ([f"{KEY_SHIFT}:0"] if terminal else []) + [f"{KEY_CTRL}:0"]
    rc, _ = await _sh("ydotool", "key", *keys)
    if enter and rc == 0:
        await asyncio.sleep(0.2)
        await _sh("ydotool", "key", f"{KEY_ENTER}:1", f"{KEY_ENTER}:0")
    await asyncio.sleep(0.4)
    if old:
        await _sh("wl-copy", input_bytes=old)
    if rc != 0:
        return {"ok": False, "error": "не получилось нажать клавиши (ydotool)"}
    return {"ok": True, "typed": len(text), "window": window, **({"sent": True} if enter else {})}


async def call(name, args, session):
    if name == "app_open":
        found = await asyncio.to_thread(_find_app, args.get("name", ""))
        if not found:
            return {"ok": False, "error": f"не нашла программу «{args.get('name')}»"}
        app_id, title = found
        subprocess.Popen(["gtk-launch", app_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        await asyncio.sleep(1.5)
        return {"ok": True, "opened": title}
    if name == "ui_elements":
        return await _helper("list")
    if name == "ui_click":
        target = args.get("name", "")
        # помощник сам смотрит на НАЙДЕННУЮ кнопку: «ок» может совпасть с «Окончательно удалить»
        res = await _helper("click", {"name": target, "risky": RISKY.pattern})
        if res.get("needs_confirm"):
            matched, window = res.get("matched") or target, res.get("window") or ""
            # после «да» — та же кнопка в том же окне; если Александр переключил окно, нажатия не будет
            return confirm.ask(f"нажать «{matched}» в окне «{window}»",
                               lambda: _helper("click", {"name": matched, "exact": True, "expect_window": window}),
                               question=f"Нажать «{matched}» в окне «{window}»?")
        return res
    if name == "ui_type":
        return await _helper("type", {"field": args.get("field", ""), "text": args.get("text", "")})
    if name == "window_action":
        shortcut = {"close": "Window Close", "minimize": "Window Minimize", "maximize": "Window Maximize"}.get(args.get("action"))
        if not shortcut:
            return {"ok": False, "error": "неизвестное действие с окном"}
        before = (await _helper("focused")).get("window", "")
        p = await asyncio.create_subprocess_exec("qdbus6", "org.kde.kglobalaccel", "/component/kwin",
                                                 "org.kde.kglobalaccel.Component.invokeShortcut", shortcut,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()
        await asyncio.sleep(0.6)
        return {"ok": p.returncode == 0, "action": args.get("action"), "window": before}
    if name == "dictate":
        text = (args.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "нечего печатать"}
        if args.get("enter"):
            return confirm.ask("напечатать и нажать Enter", lambda: _dictate(text, True),
                               question=f"Печатаю «{text[:80]}» и нажимаю Enter. Отправить?")
        return await _dictate(text, False)
    if name == "app_menu":
        # при спящем мониторе Plasma ждёт видеокарту и не отвечает на D-Bus (живой тест 2026-10-08) — будим;
        # меню открывают, чтобы им пользоваться, поэтому монитор обратно не усыпляем
        if await asyncio.to_thread(screen._dpms_is_off):
            await asyncio.to_thread(screen._run, "kscreen-doctor", "--dpms", "on", timeout=5)
            await asyncio.sleep(1.5)
        p = await asyncio.create_subprocess_exec("qdbus6", "org.kde.plasmashell", "/PlasmaShell",
                                                 "org.kde.PlasmaShell.activateLauncherMenu",
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(p.wait(), 6)
        except asyncio.TimeoutError:
            p.kill()
            return {"ok": False, "error": "панель Plasma не ответила"}
        return {"ok": p.returncode == 0, "note": "меню открыто, в нём сразу поиск: можно вписать название (ui_type)"}
    if name == "window_read":
        res = await _helper("read")
        text = (res.get("text") or "").strip() if res.get("ok") else ""
        if len(text) < 20:  # у программы нет доступности — читаем распознаванием
            return await screen.call("screen_read", {"target": "window"}, session)
        return {**screen._verbatim(text, f"окно «{res.get('window', '')}»"), "window": res.get("window")}
    return {"ok": False, "error": f"неизвестный инструмент {name}"}
