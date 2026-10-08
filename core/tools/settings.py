"""Настройки компьютера голосом (KDE Plasma): громкость, куда идёт звук, тема, ночной режим, курсор, шрифты.

Громкость, вывод звука, тема и ночной режим — сразу (легко вернуть голосом же).
Размер курсора и шрифтов Александр настраивал под своё зрение — меняются только после его «да»
(подтверждение решает ядро) и с резервной копией конфигурации.
"""
import asyncio
import os
import re
import shutil
import time

from tools import confirm

CFG = os.path.expanduser("~/.config")
BACKUP = os.path.expanduser("~/Agents/Ksenia/data/settings-backup")

SCHEMAS = [
    {"type": "function", "function": {
        "name": "setting",
        "description": ("Настройки компьютера: volume_get — какая громкость; volume_up / volume_down / volume_set (value 0–150) / mute / unmute; "
                        "audio_to (value: headphones | monitor) — куда идёт звук; theme (value: dark | light); "
                        "night_light (value: on | off) — тёплый экран вечером; cursor_size (value: 24–96); "
                        "font_size (value: размер основного шрифта 10–28; спросит «да»); restore_fonts — вернуть шрифты из копии."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["volume_get", "volume_up", "volume_down", "volume_set", "mute", "unmute", "audio_to",
                                                  "theme", "night_light", "cursor_size", "font_size", "restore_fonts"]},
            "value": {"type": "string"}}, "required": ["action"]}}},
]
TIMEOUTS = {"setting": 30}

FONT_KEYS = [("General", "font"), ("General", "menuFont"), ("General", "toolBarFont"), ("General", "fixed"),
             ("General", "smallestReadableFont"), ("WM", "activeFont")]


async def _run(*argv, timeout=15):
    p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return -1, "timeout"
    return p.returncode, out.decode("utf-8", "replace")


async def _volume():
    rc, out = await _run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
    m = re.search(r"([\d.]+)", out)
    return (round(float(m.group(1)) * 100) if m else None), "MUTED" in out


async def _sinks():
    rc, out = await _run("pactl", "list", "sinks", "short")
    return [ln.split("\t")[1] for ln in out.splitlines() if "\t" in ln]


def _backup(name):
    os.makedirs(BACKUP, exist_ok=True)
    src = os.path.join(CFG, name)
    dst = os.path.join(BACKUP, f"{name}.{time.strftime('%Y%m%d-%H%M%S')}")
    if os.path.exists(src):
        shutil.copy2(src, dst)
    return dst


async def _set_fonts(size):
    _backup("kdeglobals")
    for group, key in FONT_KEYS:
        rc, cur = await _run("kreadconfig6", "--file", "kdeglobals", "--group", group, "--key", key)
        parts = cur.strip().split(",")
        if len(parts) > 2:
            # маленький шрифт держим на 2 меньше основного, как настроено сейчас
            parts[1] = str(size - 2 if key == "smallestReadableFont" else (size + 1 if key == "activeFont" else size))
            await _run("kwriteconfig6", "--file", "kdeglobals", "--group", group, "--key", key, ",".join(parts))
    await _run("qdbus6", "org.kde.KWin", "/KWin", "reconfigure")
    return {"ok": True, "font_size": size, "note": "в уже открытых программах шрифт обновится после их перезапуска"}


async def _restore_fonts():
    files = sorted(f for f in os.listdir(BACKUP) if f.startswith("kdeglobals.")) if os.path.isdir(BACKUP) else []
    if not files:
        return {"ok": False, "error": "резервной копии шрифтов нет"}
    shutil.copy2(os.path.join(BACKUP, files[0]), os.path.join(CFG, "kdeglobals"))  # самая первая — исходная
    await _run("qdbus6", "org.kde.KWin", "/KWin", "reconfigure")
    return {"ok": True, "restored_from": files[0]}


async def _set_cursor(size):
    _backup("kcminputrc")
    rc, theme = await _run("kreadconfig6", "--file", "kcminputrc", "--group", "Mouse", "--key", "cursorTheme")
    rc, out = await _run("plasma-apply-cursortheme", theme.strip() or "breeze_cursors", "--size", str(size))
    return {"ok": rc == 0, "cursor_size": size, **({} if rc == 0 else {"error": out[-200:]})}


async def call(name, args, session):
    a, v = args.get("action"), (args.get("value") or "").strip().lower()
    if a == "volume_get":
        vol, muted = await _volume()
        sinks = await _sinks()
        rc, default = await _run("pactl", "get-default-sink")
        return {"ok": vol is not None, "volume_percent": vol, "muted": muted, "output": default.strip()}
    if a in ("volume_up", "volume_down"):
        await _run("wpctl", "set-volume", "-l", "1.5", "@DEFAULT_AUDIO_SINK@", "10%+" if a == "volume_up" else "10%-")
        vol, muted = await _volume()
        return {"ok": True, "volume_percent": vol, "muted": muted}
    if a == "volume_set":
        if not v.isdigit() or not 0 <= int(v) <= 150:
            return {"ok": False, "error": "громкость — число от 0 до 150"}
        await _run("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{int(v) / 100:.2f}")
        vol, muted = await _volume()
        return {"ok": True, "volume_percent": vol}
    if a in ("mute", "unmute"):
        await _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if a == "mute" else "0")
        return {"ok": True, "muted": a == "mute"}
    if a == "audio_to":
        sinks = await _sinks()
        want = [s for s in sinks if (s.startswith("bluez_output") if v.startswith("head") or "наушн" in v
                                     else "hdmi" in s)]
        if not want:
            return {"ok": False, "error": "такого выхода сейчас нет (наушники не подключены или монитор спит)",
                    "available": sinks}
        rc, out = await _run("pactl", "set-default-sink", want[0])
        return {"ok": rc == 0, "audio_to": want[0]}
    if a == "theme":
        scheme = {"dark": "BreezeDark", "тёмная": "BreezeDark", "light": "BreezeLight", "светлая": "BreezeLight"}.get(v)
        if not scheme:
            return {"ok": False, "error": "тема: dark или light"}
        rc, out = await _run("plasma-apply-colorscheme", scheme)
        return {"ok": rc == 0, "theme": scheme}
    if a == "night_light":
        on = v in ("on", "вкл", "включи", "true", "1")
        _backup("kwinrc")
        await _run("kwriteconfig6", "--file", "kwinrc", "--group", "NightColor", "--key", "Active", "true" if on else "false")
        await _run("qdbus6", "org.kde.KWin", "/KWin", "reconfigure")
        return {"ok": True, "night_light": on}
    if a == "cursor_size":
        if not v.isdigit() or not 24 <= int(v) <= 96:
            return {"ok": False, "error": "размер курсора — от 24 до 96"}
        size = int(v)
        return confirm.ask(f"поставить размер курсора {size}", lambda: _set_cursor(size), f"Поставить размер курсора {size}?")
    if a == "font_size":
        if not v.isdigit() or not 10 <= int(v) <= 28:
            return {"ok": False, "error": "размер шрифта — от 10 до 28"}
        size = int(v)
        return confirm.ask(f"поставить размер шрифта {size}", lambda: _set_fonts(size),
                           f"Поставить основной шрифт размером {size}? Старые настройки я сохраню.")
    if a == "restore_fonts":
        return confirm.ask("вернуть прежние шрифты", _restore_fonts, "Вернуть прежние шрифты из копии?")
    return {"ok": False, "error": f"неизвестная настройка {a}"}
