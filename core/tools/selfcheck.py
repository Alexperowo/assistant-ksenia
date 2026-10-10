"""Самодиагностика Ксении: «ты в порядке?», «что у тебя сломалось?».

Проверяет свои части и окружение и отдаёт модели простой список «что не так и что делать» —
Александр не видит экран, поэтому неисправность должна быть сказана словами.
"""
import asyncio
import json
import os
import shutil

import aiohttp

CFG = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json"), encoding="utf-8"))
SERVICES = {"ksenia-brain": "мозг", "ksenia-voice-out": "голос", "ksenia-voice-in": "слух", "ksenia-judge": "судья реплик",
            "ksenia-core": "ядро"}
GPU_UNITS = {"ksenia-brain": "мозг", "ksenia-voice-out": "голос", "ksenia-judge": "судья реплик"}

SCHEMAS = [
    {"type": "function", "function": {
        "name": "self_check",
        "description": ("Проверить себя: мозг, голос, слух, браузер, наушники, место на диске, видеокарты. "
                        "Вызывай на «ты в порядке?», «что сломалось?», «почему не работает …»."),
        "parameters": {"type": "object", "properties": {}}}},
]
TIMEOUTS = {"self_check": 40}


async def _run(*argv):
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        return p.returncode, out.decode("utf-8", "replace").strip()
    except Exception as e:
        return -1, repr(e)


async def _http(session, url, headers=None):
    try:
        async with session.get(url, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=5)) as r:
            return r.status
    except Exception:
        return None


async def call(name, args, session):
    problems, fine = [], []
    for unit, what in SERVICES.items():
        rc, st = await _run("systemctl", "--user", "is-active", unit)
        (fine if st == "active" else problems).append(
            f"{what}: работает" if st == "active" else f"{what} ({unit}) не работает — «{st}»; поможет перезапуск службы")
    key = ""
    try:
        key = open(CFG["brain_key_file"], encoding="utf-8").read().strip()
    except OSError:
        problems.append("нет ключа мозга")
    brain_down = await _http(session, CFG["brain_url"] + "/health", {"Authorization": "Bearer " + key}) != 200
    if brain_down:
        problems.append("мозг не отвечает на проверку (возможно, ещё загружается — до минуты после запуска)")
    restart = []
    rc, apps = await _run("nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader")
    on_gpu = set(apps.split()) if rc == 0 else None
    for unit, what in GPU_UNITS.items():
        rc, pid = await _run("systemctl", "--user", "show", "-p", "MainPID", "--value", unit)
        # служба работает, а её процесса нет на видеокарте: драйвер не был готов при запуске, и движок молча
        # ушёл на процессор (2026-10-10: мозг отвечал по 3 минуты). Мозг загружается до минуты — пока он не
        # отвечает на /health, не считаем (иначе ложная тревога)
        if on_gpu is None or pid in ("", "0") or pid in on_gpu:
            continue
        if unit == "ksenia-brain" and brain_down:
            continue
        problems.append(f"{what} работает без видеокарты — очень медленно; поможет перезапуск службы {unit}")
        restart.append(unit)
    rc, sinks = await _run("pactl", "list", "sinks", "short")
    if "bluez_output" in sinks:
        fine.append("наушники подключены")
    else:
        problems.append("наушники не подключены — голос пойдёт в монитор или в никуда; включи наушники")
    rc, hs = await _run("curl", "-s", "-m", "5", "http://127.0.0.1:18120/headset")
    if '"mode": "talk"' in hs:
        fine.append("наушники в режиме разговора — можно перебивать")
    elif '"mode": "music"' in hs:
        fine.append("наушники в режиме музыки — слушаю после сигнала")
    if '"не удалось' in hs:
        problems.append("канал звука наушников недавно не поднялся — если звука нет, выключи и включи наушники")
    rc, st = await _run("curl", "-s", "-m", "3", "http://127.0.0.1:18120/status")
    if '"busy": true' in st:
        fine.append("слух сейчас занят записью")
    total, used, free = shutil.disk_usage("/")
    gb = free / 1e9
    (problems if gb < 10 else fine).append(f"свободно на диске {gb:.0f} ГБ" + (" — мало, стоит почистить" if gb < 10 else ""))
    rc, gpu = await _run("nvidia-smi", "--query-gpu=index,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits")
    for ln in gpu.splitlines():
        parts = [x.strip() for x in ln.split(",")]
        if len(parts) == 4 and parts[3].isdigit():
            idx, used_mb, total_mb, temp = parts
            name_ = "видеокарта мозга" if idx == "0" else "видеокарта голоса"
            if int(temp) >= 85:
                problems.append(f"{name_} горячая: {temp}°")
            if int(used_mb) > int(total_mb) * 0.97:
                problems.append(f"{name_}: память почти кончилась")
    rc, mem = await _run("free", "-m")
    for ln in mem.splitlines():
        if ln.startswith(("Mem:", "Память:")):
            avail = int(ln.split()[-1])
            if avail < 3000:
                problems.append(f"мало свободной оперативной памяти: {avail // 1024} ГБ")
    if not os.path.exists(os.path.expanduser("~/Agents/Ksenia/secrets/yandex_music_token")):
        problems.append("Яндекс Музыка не подключена (нет ключа)")
    return {"ok": True, "problems": problems, "fine": fine, "restart": restart,
            "note": "скажи Александру коротко: всё ли в порядке; если есть проблемы — что именно и что сделать, простыми словами"}
