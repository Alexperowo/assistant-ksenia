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
import io
import os
import re
import subprocess
import time

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
        "name": "screen_click",
        "description": ("Нажать на экране надпись, которую видно (кнопка, ссылка, пункт) — когда ui_click не находит "
                        "элемент (программа без доступности, сайт, игра). text — надпись как на экране; "
                        "nth — какая по счёту, если одинаковых несколько. Рискованное (удалить, отправить…) — спросит «да»."),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}, "nth": {"type": "integer"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "cursor_look",
        "description": "Где курсор мыши и что под ним (рассказать словами).",
        "parameters": {"type": "object", "properties": {}}}},
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

TIMEOUTS = {"screen_click": 60, "cursor_look": 60, "dictate": 20, "app_menu": 10, "window_action": 15, "app_open": 20, "ui_elements": 25, "ui_click": 25, "ui_type": 25, "window_read": 60}


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


# ---------- мышь по зрению: где курсор (KWin), наведение с обратной связью, клик ----------
# ydotool в этой версии не умеет абсолютных координат (курсор улетает в угол), а относительные движения
# проходят через ускорение указателя (100 -> 183 точки) — поэтому наводимся как рукой: сдвинуть, проверить,
# поправить (2–4 шага). Координаты KWin — логические (экран 3840x2160 при масштабе 2 — это 1920x1080).
_KWIN_CURSOR_JS = os.path.join(ROOT, "..", "..", "data", "kwin-cursor.js")


async def _cursor():
    """Где курсор (логические координаты KWin). Скрипт KWin печатает в журнал строку с меткой этого замера —
    берём только её (иначе можно прочитать позицию из предыдущего замера)."""
    nonce = f"{time.time_ns() % 10**9}"
    os.makedirs(os.path.dirname(_KWIN_CURSOR_JS), exist_ok=True)
    with open(_KWIN_CURSOR_JS, "w") as f:
        f.write(f'print("KSENIA_CURSOR {nonce} " + workspace.cursorPos.x + " " + workspace.cursorPos.y);\n')
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 1))
    rc, out = await _sh("qdbus6", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.loadScript", _KWIN_CURSOR_JS,
                        "ksenia-cursor")
    sid = out.decode().strip()
    await _sh("qdbus6", "org.kde.KWin", f"/Scripting/Script{sid}", "org.kde.kwin.Script.run")
    await _sh("qdbus6", "org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting.unloadScript", "ksenia-cursor")
    for _ in range(10):
        rc, log_ = await _sh("journalctl", "--user", "--since", since, "-o", "cat", "--no-pager", timeout=5)
        m = re.findall(rf"KSENIA_CURSOR {nonce} (-?\d+) (-?\d+)", log_.decode("utf-8", "replace"))
        if m:
            return int(m[-1][0]), int(m[-1][1])
        await asyncio.sleep(0.05)
    return None


_flat = {"done": False}


async def _ensure_flat_pointer():
    """Ускорение указателя для виртуальной мыши ydotool — выключить (1:1), иначе сдвиг 100 становится 183.
    Сохранено и в kcminputrc (переживает перезагрузку); здесь — на случай, если устройство пересоздано."""
    if _flat["done"]:
        return
    rc, out = await _sh("qdbus6", "org.kde.KWin")
    for path in re.findall(r"/org/kde/KWin/InputDevice/event\d+", out.decode()):
        rc, nm = await _sh("busctl", "--user", "get-property", "org.kde.KWin", path, "org.kde.KWin.InputDevice", "name")
        if b"ydotool" in nm:
            await _sh("busctl", "--user", "set-property", "org.kde.KWin", path, "org.kde.KWin.InputDevice",
                      "pointerAccelerationProfileFlat", "b", "true")
            _flat["done"] = True


async def _move_to(x, y, tries=4):
    await _ensure_flat_pointer()
    for _ in range(tries):
        cur = await _cursor()
        if cur is None:
            return False
        dx, dy = x - cur[0], y - cur[1]
        if abs(dx) <= 2 and abs(dy) <= 2:
            return True
        await _sh("ydotool", "mousemove", "-x", str(dx), "-y", str(dy))
        await asyncio.sleep(0.08)
    cur = await _cursor()
    return cur is not None and abs(cur[0] - x) <= 3 and abs(cur[1] - y) <= 3


def _screen_scale():
    p = subprocess.run(["kscreen-doctor", "-o"], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r"Scale:\S*\s*([\d.]+)", re.sub(r"\x1b\[[0-9;]*m", "", p))
    return float(m.group(1)) if m else 1.0


def _find_text(im, target, nth=1):
    """Надпись на снимке (Tesseract TSV): центр совпадения в пикселях снимка или None. Ищет подряд идущие слова."""
    buf = io.BytesIO()
    im.save(buf, "PNG")
    tsv = subprocess.run(["tesseract", "stdin", "stdout", "-l", "rus+eng", "--psm", "11", "tsv"], input=buf.getvalue(),
                         capture_output=True, timeout=60).stdout.decode("utf-8", "ignore")
    words = []
    for ln in tsv.splitlines()[1:]:
        c = ln.split("\t")
        if len(c) >= 12 and c[11].strip() and float(c[10] or -1) > 30:
            words.append((_norm(c[11]), int(c[6]), int(c[7]), int(c[8]), int(c[9]), (c[2], c[3], c[4])))
    want = _norm(target).split()
    if not want:
        return None
    hits = []
    for i in range(len(words)):
        seq = words[i:i + len(want)]
        if len(seq) == len(want) and all(w[0].startswith(t[:max(3, len(t) - 1)]) or t.startswith(w[0]) and len(w[0]) >= 3
                                         for w, t in zip(seq, want)):
            x0, y0 = min(w[1] for w in seq), min(w[2] for w in seq)
            x1, y1 = max(w[1] + w[3] for w in seq), max(w[2] + w[4] for w in seq)
            hits.append(((x0 + x1) // 2, (y0 + y1) // 2))
    hits.sort(key=lambda p: (p[1] // 20, p[0]))  # сверху вниз, слева направо
    return hits[nth - 1] if 0 < nth <= len(hits) else None


async def _click_at(lx, ly):
    if not await _move_to(lx, ly):
        return {"ok": False, "error": "не получилось навести курсор"}
    rc, _ = await _sh("ydotool", "click", "0xC0")
    return {"ok": rc == 0, "clicked_at": [lx, ly]}


async def _screen_click(text, nth):
    if await asyncio.to_thread(screen.zoom_active):
        return {"ok": False, "error": "включена экранная лупа — сначала уменьши её до конца (magnifier zoom_out), "
                                      "иначе я не попаду в нужное место"}
    im = await asyncio.to_thread(screen._capture, "screen")
    pt = await asyncio.to_thread(_find_text, im, text, nth)
    if pt is None:
        return {"ok": False, "error": f"не вижу на экране надписи «{text}»"}
    scale = await asyncio.to_thread(_screen_scale)
    lx, ly = round(pt[0] / scale), round(pt[1] / scale)
    if RISKY.search(text):
        return confirm.ask(f"нажать «{text}» на экране", lambda: _click_at(lx, ly), question=f"Нажать «{text}»?")
    return await _click_at(lx, ly)


def _text_near(im, cx, cy):
    """Текст прямо под курсором (Tesseract по узкой полосе вокруг него) — точнее, чем зрение."""
    box = (max(0, cx - 260), max(0, cy - 60), min(im.size[0], cx + 260), min(im.size[1], cy + 60))
    buf = io.BytesIO()
    im.crop(box).save(buf, "PNG")
    out = subprocess.run(["tesseract", "stdin", "stdout", "-l", "rus+eng", "--psm", "6"], input=buf.getvalue(),
                         capture_output=True, timeout=30).stdout.decode("utf-8", "ignore")
    return " ".join(w for w in out.split() if len(w) > 1 or w.isalnum())[:200]


async def _cursor_look(session):
    cur = await _cursor()
    if cur is None:
        return {"ok": False, "error": "не узнала, где курсор"}
    if await asyncio.to_thread(screen.zoom_active):
        return {"ok": False, "error": "включена лупа — координаты курсора не совпадают со снимком; уменьши лупу"}
    im = await asyncio.to_thread(screen._capture, "screen")
    scale = await asyncio.to_thread(_screen_scale)
    cx, cy = int(cur[0] * scale), int(cur[1] * scale)
    text = await asyncio.to_thread(_text_near, im, cx, cy)
    w, h = im.size
    box = (max(0, cx - 500), max(0, cy - 300), min(w, cx + 500), min(h, cy + 300))
    crop = im.crop(box).copy()
    from PIL import ImageDraw  # яркое кольцо там, где курсор: зрению нужно знать, куда смотреть
    d = ImageDraw.Draw(crop)
    px, py = cx - box[0], cy - box[1]
    d.ellipse((px - 40, py - 40, px + 40, py + 40), outline=(255, 0, 0), width=8)
    seen = await screen._vision(crop, "Что находится внутри красного кольца (там курсор мыши)? Кнопка, ссылка, значок, "
                                      "поле, текст? Назови надпись, если есть. Одна-две фразы.", session)
    where = ("вверху" if cur[1] < 360 else "внизу" if cur[1] > 720 else "посередине") + " " + \
            ("слева" if cur[0] < 640 else "справа" if cur[0] > 1280 else "по центру")
    return {"ok": True, "where": where, "text_under_cursor": text, "seen": seen,
            "note": "главное — seen (зрение смотрит в кольцо вокруг курсора); text_under_cursor — распознанный текст рядом, "
                    "на фоне картинок бывает мусором — используй только если совпадает по смыслу"}


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
    if name == "screen_click":
        return await _screen_click(args.get("text", ""), max(1, int(args.get("nth") or 1)))
    if name == "cursor_look":
        return await _cursor_look(session)
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
