"""Помощник новичка в Linux: КАТАЛОГ ПРОВЕРЕННЫХ КОМАНД вместо произвольного терминала.

Решение Александра (2026-10-08): песочница бесполезна для помощи с настоящей системой, поэтому —
список проверенных команд:
  info   — только чтение, выполняется сразу (диск, память, что тормозит, температура, сеть, обновления…);
  change — меняет систему, выполняет ЯДРО после «да» Александра (tools/confirm): установка/удаление программ,
           обновление системы, перезапуск зависшей службы, очистка мусора;
  всего, чего нет в каталоге, Ксения не выполняет.
Команды запускаются без оболочки (argv), параметры проверяются строгими шаблонами — в название программы
нельзя «подсунуть» другую команду. Вывод урезан и отдаётся модели для пересказа простыми словами.
"""
import asyncio
import os
import re
import shutil

from tools import confirm

PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,62}$")          # имя пакета apt/dnf
UNIT_RE = re.compile(r"^[A-Za-z0-9@_.\-]{1,80}\.service$")
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$", re.I)
OUT_MAX = 3000

PM = "apt" if shutil.which("apt-get") else ("dnf" if shutil.which("dnf") else None)


def _apt(*a):
    # conffile: оставить свой файл настроек, не спрашивать (вопрос dpkg без терминала повесил бы установку)
    return ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y", "-q",
            "-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold", *a]


# Части системы, без которых нет рабочего стола, звука, сети или загрузки: их не удаляет ни одна команда
# (ни прямо, ни как зависимость). Префиксы: «pipewire» закрывает и pipewire-pulse, и pipewire-audio.
VITAL = ("sudo", "systemd", "plasma-", "kwin", "kubuntu-", "linux-image", "linux-generic", "network-manager",
         "pipewire", "wireplumber", "bluez", "nvidia-", "apt", "dpkg", "python3", "openssh-server", "sddm", "libc6",
         "ubuntu-minimal", "ubuntu-standard", "xdg-desktop-portal", "dbus", "polkit", "grub", "shim", "cuda")


def _vital(name: str) -> bool:
    name = name.split(":")[0]
    return any(name == v or (v.endswith("-") and name.startswith(v)) or name.startswith(v + "-") or name == v
               for v in VITAL)


async def _simulate(*a):
    """Что apt сделает на самом деле: «pipewire-» в install — это УДАЛИТЬ pipewire (и рабочий стол с ним)."""
    rc, out = await _exec(["apt-get", "-s", "-q", *a], 60)
    removes = [ln.split()[1] for ln in out.splitlines() if ln.startswith("Remv ")]
    return rc, removes, out


INFO = {
    "overview": ("Общая сводка: загрузка, что тормозит, память, видеокарты, температуры, диски, сбои",
                 lambda p: [os.path.expanduser("~/.local/bin/nexus-diag")]),
    "disk": ("Место на дисках", lambda p: ["df", "-h", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs", "-x", "efivarfs"]),
    "big_folders": ("Самые большие папки в домашней папке", lambda p: ["du", "-xh", "-d", "1", os.path.expanduser("~")]),
    "memory": ("Оперативная память", lambda p: ["free", "-h"]),
    "top_cpu": ("Какие программы сейчас грузят процессор", lambda p: ["ps", "-eo", "comm,pcpu,pmem", "--sort=-pcpu", "--no-headers"]),
    "top_memory": ("Какие программы занимают больше всего памяти", lambda p: ["ps", "-eo", "comm,rss", "--sort=-rss", "--no-headers"]),
    "temperatures": ("Температуры", lambda p: ["sensors"]),
    "gpu": ("Видеокарты: память, загрузка, температура", lambda p: ["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu", "--format=csv,noheader"]),
    "uptime": ("Сколько работает компьютер и нагрузка", lambda p: ["uptime"]),
    "os_version": ("Версия системы", lambda p: ["lsb_release", "-ds"]),
    "updates": ("Есть ли обновления системы", lambda p: ["/usr/lib/update-notifier/apt-check"] if PM == "apt" else ["dnf", "check-update", "-q"]),
    "failed_services": ("Сломавшиеся службы", lambda p: ["systemctl", "--failed", "--no-legend", "--plain"]),
    "failed_user_services": ("Сломавшиеся службы пользователя", lambda p: ["systemctl", "--user", "--failed", "--no-legend", "--plain"]),
    "errors": ("Последние ошибки в журнале системы", lambda p: ["journalctl", "-p", "3", "-b", "--no-pager", "-n", "25", "-o", "short"]),
    "network": ("Сеть: подключения и адреса", lambda p: ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]),
    "wifi_list": ("Доступные сети Wi-Fi", lambda p: ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list"]),
    "internet": ("Есть ли интернет", lambda p: ["nmcli", "networking", "connectivity", "check"]),
    "bluetooth": ("Bluetooth-устройства: известные и подключённые", lambda p: ["bluetoothctl", "devices"]),
    "batteries": ("Заряд беспроводных устройств (наушники, мышь, планшет)", lambda p: ["upower", "-d"]),  # разбор — _batteries
    "audio_outputs": ("Куда сейчас идёт звук, устройства звука", lambda p: ["wpctl", "status"]),
    "is_installed": ("Установлена ли программа (param: имя пакета)", lambda p: ["dpkg-query", "-W", "-f=${Status} ${Version}\\n", p] if PM == "apt" else ["rpm", "-q", p]),
    "search_package": ("Найти программу в репозитории (param: слово)", lambda p: ["apt-cache", "search", "--names-only", p] if PM == "apt" else ["dnf", "search", "-q", p]),
}

CHANGE = {
    "install": ("Установить программу (param: имя пакета)", "установить «{p}»",
                lambda p: _apt("install", "--no-remove", p) if PM == "apt" else ["sudo", "-n", "dnf", "-y", "install", p], 900),
    "remove": ("Удалить программу (param: имя пакета; данные пользователя не трогаются)", "удалить программу «{p}»",
               lambda p: _apt("remove", p) if PM == "apt" else ["sudo", "-n", "dnf", "-y", "remove", p], 600),
    "update_system": ("Обновить систему целиком", "обновить систему (может занять долго)",
                      lambda p: None, 3600),
    "clean": ("Почистить мусор: кэш пакетов и старые журналы", "почистить кэш пакетов и старые журналы",
              lambda p: None, 600),
    "restart_user_service": ("Перезапустить службу пользователя (param: имя .service)", "перезапустить службу «{p}»",
                             lambda p: ["systemctl", "--user", "restart", p], 60),
    "bluetooth_connect": ("Подключить известное Bluetooth-устройство (param: MAC из списка bluetooth)",
                          "подключить Bluetooth-устройство {p}", lambda p: ["bluetoothctl", "connect", p], 30),
}

PARAM_RULES = {"is_installed": PKG_RE, "search_package": re.compile(r"^[\w+.\- ]{2,40}$"), "install": PKG_RE,
               "remove": PKG_RE, "restart_user_service": UNIT_RE, "bluetooth_connect": MAC_RE}
PROTECTED = {"sudo", "systemd", "plasma-desktop", "kwin-wayland", "linux-image-generic", "network-manager",
             "pipewire", "bluez", "nvidia-driver", "apt", "dpkg", "python3", "openssh-server", "sddm"}

SCHEMAS = [
    {"type": "function", "function": {
        "name": "system",
        "description": ("Компьютер Александра (он новичок в Linux): ТОЛЬКО команды из каталога. "
                        "Справочные: " + "; ".join(f"{k} — {v[0]}" for k, v in INFO.items()) + ". "
                        "Меняющие систему (спросят «да» у Александра сами): " + "; ".join(f"{k} — {v[0]}" for k, v in CHANGE.items()) + ". "
                        "Результат объясни простыми словами, без терминов. Если нужной команды нет — честно скажи."),
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "enum": list(INFO) + list(CHANGE)},
            "param": {"type": "string", "description": "параметр, если команда его требует"}},
            "required": ["command"]}}},
]
TIMEOUTS = {"system": 60}


async def _exec(argv, timeout):
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                 env={**os.environ, "LC_ALL": "C.UTF-8", "COLUMNS": "160"})
    except FileNotFoundError:
        return 127, f"нет программы {argv[0]}"
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        p.kill()
        return -1, "не уложилось во время"
    text = out.decode("utf-8", "replace")
    return p.returncode, text


def _trim(cmd, text):
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if cmd in ("top_cpu", "top_memory"):
        lines = lines[:10]
    if cmd == "big_folders":
        def size(ln):
            m = re.match(r"([\d.,]+)([KMGT]?)", ln.split()[0] if ln.split() else "")
            if not m:
                return 0
            return float(m.group(1).replace(",", ".")) * {"": 1e-6, "K": 1e-3, "M": 1, "G": 1e3, "T": 1e6}[m.group(2)]
        lines = sorted(lines, key=size, reverse=True)[:12]
    if cmd == "batteries":
        return _batteries(text)
    if cmd == "updates" and PM == "apt":
        m = re.search(r"(\d+);(\d+)", text)  # apt-check: «всего;из них безопасности» (без рекламы ESM)
        if m:
            return f"обновлений можно поставить: {m.group(1)}, из них важных для безопасности: {m.group(2)}"
    text = "\n".join(lines)
    return text[-OUT_MAX:] if cmd == "errors" else text[:OUT_MAX]


KINDS = {"mouse": "мышь", "keyboard": "клавиатура", "headset": "наушники", "headphones": "наушники",
         "phone": "телефон", "tablet": "планшет", "battery": "батарея компьютера", "gaming-input": "геймпад",
         "touchpad": "тачпад", "speakers": "колонка", "other": "устройство"}


def _batteries(text):
    """upower -d: по каждому устройству — что это (мышь/наушники…), модель и заряд; DisplayDevice пропускаем."""
    out = []
    for block in text.split("Device: ")[1:]:
        if "DisplayDevice" in block.split("\n")[0]:
            continue
        model = re.search(r"model:\s*(.+)", block)
        pct = re.search(r"percentage:\s*([\d.]+)%(.*)", block)
        kind = next((KINDS.get(ln.strip(), ln.strip()) for ln in block.splitlines()[1:20]
                     if re.fullmatch(r"\s+[a-z-]+", ln)), "устройство")
        unreliable = pct and "ignored" in pct.group(2)
        out.append(f"{kind}: {model.group(1).strip() if model else 'без названия'} — "
                   + (f"заряд {pct.group(1)}%" + (" (данные ненадёжны — устройство не сообщает точный заряд)" if unreliable else "")
                      if pct else "заряд неизвестен"))
    return "\n".join(out) or "устройств с батареей не видно (наушники не подключены?)"


async def _run_change(cmd, p):
    """Выполняется только ядром после «да» Александра."""
    if cmd == "update_system":
        if PM != "apt":
            rc, out = await _exec(["sudo", "-n", "dnf", "-y", "upgrade"], 3600)
        else:
            rc, out = await _exec(_apt("update"), 600)
            if rc == 0:  # upgrade, а не full-upgrade: обновление ничего не удаляет
                rc, out = await _exec(_apt("upgrade", "--with-new-pkgs", "--no-remove"), 3600)
    elif cmd == "clean":
        rc, out = await _exec(_apt("autoremove", "--purge") if PM == "apt" else ["sudo", "-n", "dnf", "-y", "autoremove"], 600)
        await _exec(_apt("clean") if PM == "apt" else ["sudo", "-n", "dnf", "clean", "all"], 120)
        await _exec(["sudo", "-n", "journalctl", "--vacuum-time=14d"], 120)
    else:
        argv = CHANGE[cmd][2](p)
        rc, out = await _exec(argv, CHANGE[cmd][3])
    tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-12:])
    return {"ok": rc == 0, "command": cmd, "param": p, "exit_code": rc, "output_tail": tail[:1500]}


async def call(name, args, session):
    cmd = args.get("command")
    p = (args.get("param") or "").strip()
    if cmd not in INFO and cmd not in CHANGE:
        return {"ok": False, "error": "такой команды нет в каталоге проверенных — Ксения её не выполняет"}
    rule = PARAM_RULES.get(cmd)
    if rule is not None and not rule.match(p):
        return {"ok": False, "error": f"параметр «{p}» не подходит для команды {cmd}"}
    if cmd in ("install", "remove") and p in PROTECTED:
        return {"ok": False, "error": f"«{p}» — важная часть системы, её Ксения не трогает"}
    if cmd in INFO:
        rc, out = await _exec(INFO[cmd][1](p), 45)
        if cmd == "updates" and PM == "apt":  # apt-check пишет в stderr и возвращает 0
            rc = 0
        return {"ok": rc in (0, 1) if cmd in ("is_installed", "search_package") else rc == 0,
                "command": cmd, "output": _trim(cmd, out) or "(пусто)",
                "note": "объясни Александру простыми словами главное, без терминов и цифр-простыней"}
    label = CHANGE[cmd][1].format(p=p)
    question = f"{label[:1].upper()}{label[1:]}?"
    if PM == "apt" and cmd in ("install", "remove", "clean"):
        sim = {"install": ("install", p), "remove": ("remove", p), "clean": ("autoremove",)}[cmd]
        rc, removes, out = await _simulate(*sim)
        if rc != 0:
            return {"ok": False, "error": "проверка показала, что так не получится",
                    "detail": "\n".join(out.splitlines()[-3:])[:300]}
        vital = [r for r in removes if _vital(r)]
        if vital:
            return {"ok": False, "error": "это удалило бы важные части системы — Ксения так не делает",
                    "would_remove": vital[:5]}
        if cmd == "install" and removes:
            return {"ok": False, "error": "установка потребовала бы удалить другие программы — Ксения так не делает",
                    "would_remove": removes[:5]}
        if cmd == "remove" and len(removes) > 1:
            question = (f"Удалить программу «{p}»? Вместе с ней удалятся ещё {len(removes) - 1}: "
                        f"{', '.join(r for r in removes if r != p)[:120]}. Удалить?")
        if cmd == "clean" and not removes:
            label, question = "почистить кэш пакетов и старые журналы", "Почистить кэш пакетов и старые журналы?"
    limit = {"install": 900, "remove": 600, "update_system": 4200, "clean": 900}.get(cmd, 60)
    return confirm.ask(label, lambda: _run_change(cmd, p), question, limit=limit,
                       background=cmd in ("install", "update_system", "clean"))
