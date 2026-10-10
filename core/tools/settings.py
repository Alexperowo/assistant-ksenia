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
                        "font_size (value: размер основного шрифта 10–28; спросит «да»); restore_fonts — вернуть шрифты из копии; "
                        "brightness_get / brightness_up / brightness_down / brightness_set (value 0–100) — яркость монитора; "
                        "wifi_status — к какой сети подключён и сигнал; wifi_list — какие сети рядом; "
                        "wifi_connect (value: имя сети; password — если сеть новая; спросит «да»)."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["volume_get", "volume_up", "volume_down", "volume_set", "mute", "unmute", "audio_to",
                                                  "theme", "night_light", "cursor_size", "font_size", "restore_fonts",
                                                  "brightness_get", "brightness_up", "brightness_down", "brightness_set",
                                                  "wifi_status", "wifi_list", "wifi_connect"]},
            "value": {"type": "string"}, "password": {"type": "string"}}, "required": ["action"]}}},
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


MIN_VOLUME = 10


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


_ddc = {"bus": None}


async def _brightness(set_to=None, delta=None):
    """Яркость монитора по DDC/CI (ddcutil). Шина находится один раз."""
    if _ddc["bus"] is None:
        rc, out = await _run("ddcutil", "detect", "--brief", timeout=30)
        m = re.search(r"I2C bus:\s*/dev/i2c-(\d+)", out)
        if not m:
            return {"ok": False, "error": "монитор не отвечает на управление яркостью"}
        _ddc["bus"] = m.group(1)
    rc, out = await _run("ddcutil", "--bus", _ddc["bus"], "getvcp", "10", timeout=20)
    m = re.search(r"current value =\s*(\d+), max value =\s*(\d+)", out)
    if not m:
        return {"ok": False, "error": "не смогла узнать яркость"}
    cur, mx = int(m.group(1)), int(m.group(2))
    if set_to is None and delta is None:
        return {"ok": True, "brightness": round(cur * 100 / mx)}
    new = set_to if set_to is not None else round(cur * 100 / mx) + delta
    new = max(0, min(100, new))
    rc, out = await _run("ddcutil", "--bus", _ddc["bus"], "setvcp", "10", str(round(new * mx / 100)), timeout=20)
    return {"ok": rc == 0, "brightness": new, **({} if rc == 0 else {"error": out[-200:]})}


def _fields(line: str):
    """Строка nmcli -t: поля через «:», а «:» внутри имени сети экранировано как «\\:» (аудит Fable, C12)."""
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in re.split(r"(?<!\\):", line)]


async def _wifi_status():
    rc, out = await _run("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi")
    for ln in out.splitlines():
        parts = _fields(ln)
        if len(parts) >= 3 and parts[0] == "yes":
            return {"ok": True, "ssid": parts[1], "signal_percent": int(parts[2] or 0)}
    return {"ok": True, "ssid": None, "note": "Wi-Fi не подключён"}


async def _online(timeout=30):
    for _ in range(timeout // 3):
        rc, out = await _run("nmcli", "networking", "connectivity", "check", timeout=10)
        if out.strip() == "full":
            return True
        if out.strip() == "unknown":  # проверка связи выключена в NetworkManager — смотрим, подключён ли он
            rc, st = await _run("nmcli", "-t", "-f", "STATE", "general", timeout=10)
            if st.strip() in ("connected", "подключено"):
                return True
        await asyncio.sleep(3)
    return False


async def _wifi_connect(ssid, password):
    """Подключиться; нет интернета за 30 с — вернуться на прежнюю сеть (это единственная связь компьютера)."""
    before = (await _wifi_status()).get("ssid")
    rc, names = await _run("nmcli", "-t", "-f", "NAME", "con", "show", timeout=10)
    had_profile = ssid in [_fields(n)[0] for n in names.splitlines()]
    argv = ["nmcli", "dev", "wifi", "connect", ssid] + (["password", password] if password else [])
    rc, out = await _run(*argv, timeout=45)
    if rc == 0 and await _online():
        return {"ok": True, "connected": ssid}
    if not had_profile:  # профиль с неверным паролем не оставляем — он мешал бы следующей попытке
        await _run("nmcli", "con", "delete", "id", ssid, timeout=15)
    if before and before != ssid:
        await _run("nmcli", "con", "up", "id", before, timeout=45)
        await _online()
    return {"ok": False, "error": f"к «{ssid}» не получилось" + (f" — вернулась на «{before}»" if before else ""),
            "detail": out[-200:]}


async def call(name, args, session):
    a, v = args.get("action"), (args.get("value") or "").strip().lower()
    if a == "volume_get":
        vol, muted = await _volume()
        sinks = await _sinks()
        rc, default = await _run("pactl", "get-default-sink")
        return {"ok": vol is not None, "volume_percent": vol, "muted": muted, "output": default.strip()}
    if a in ("volume_up", "volume_down"):
        vol0, _ = await _volume()
        if a == "volume_down" and vol0 is not None and vol0 <= MIN_VOLUME:
            return {"ok": True, "volume_percent": vol0, "note": "уже самый тихий уровень, при котором ты меня слышишь"}
        await _run("wpctl", "set-volume", "-l", "1.5", "@DEFAULT_AUDIO_SINK@", "10%+" if a == "volume_up" else "10%-")
        vol, muted = await _volume()
        return {"ok": True, "volume_percent": vol, "muted": muted}
    if a == "volume_set":
        if not v.isdigit() or not 0 <= int(v) <= 150:
            return {"ok": False, "error": "громкость — число от 0 до 150"}
        # не тише 10 %: через этот же выход говорит сама Ксения — Александр не видит экран и остался бы
        # совсем без голоса (аудит Fable, C10); тишину для музыки — music_control pause
        level = max(int(v), MIN_VOLUME)
        await _run("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level / 100:.2f}")
        vol, muted = await _volume()
        return {"ok": True, "volume_percent": vol,
                **({"note": f"тише {MIN_VOLUME} % не ставлю — иначе ты меня не услышишь"} if level != int(v) else {})}
    if a in ("mute", "unmute"):
        if a == "mute":
            # «выключи звук» глушил и саму Ксению: глушим музыку (пауза), а голос остаётся
            from tools import music
            await music.call("music_control", {"action": "pause"}, session)
            return {"ok": True, "music_paused": True,
                    "note": "звук компьютера совсем не выключаю — иначе ты не услышишь меня; музыку поставила на паузу"}
        await _run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0")
        return {"ok": True, "muted": False}
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
    if a == "brightness_get":
        return await _brightness()
    if a in ("brightness_up", "brightness_down"):
        return await _brightness(delta=15 if a == "brightness_up" else -15)
    if a == "brightness_set":
        if not v.isdigit() or not 0 <= int(v) <= 100:
            return {"ok": False, "error": "яркость — число от 0 до 100"}
        return await _brightness(set_to=int(v))
    if a == "wifi_status":
        return await _wifi_status()
    if a == "wifi_list":
        rc, out = await _run("nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list")
        nets, seen = [], set()
        for ln in out.splitlines():
            parts = ln.rsplit(":", 2)
            if len(parts) == 3 and parts[0] and parts[0] not in seen:
                seen.add(parts[0])
                nets.append({"ssid": parts[0], "signal_percent": int(parts[1] or 0), "open": parts[2] in ("", "--")})
        return {"ok": True, "networks": sorted(nets, key=lambda x: -x["signal_percent"])[:6]}
    if a == "wifi_connect":
        ssid = (args.get("value") or "").strip()
        if not ssid:
            return {"ok": False, "error": "к какой сети?"}
        pw = args.get("password") or ""
        return confirm.ask(f"переключить Wi-Fi на «{ssid}»", lambda: _wifi_connect(ssid, pw),
                           question=f"Переключаю Wi-Fi на «{ssid}». Связь пропадёт на полминуты; если интернета не будет — "
                                    f"вернусь на прежнюю сеть. Переключить?")
    return {"ok": False, "error": f"неизвестная настройка {a}"}
