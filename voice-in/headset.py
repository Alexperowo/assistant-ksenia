"""Наушники: здоровье канала LE Audio, восстановление и режимы «музыка» / «разговор».

Живой тест 2026-10-09: после перезагрузки наушники подключились, но канал звука LE Audio не поднялся
(bluetoothd: iso_connect_cb … Device or resource busy; WirePlumber: Failure in Bluetooth audio transport) —
тишина в обе стороны. Помогло переподключение, а в упорном случае — перезапуск службы Bluetooth.
Здесь это делается само: проверка при запуске слуха, слежение за журналом WirePlumber, восстановление по шагам.

Режимы:
  talk  — LE Audio (bap-duplex, LC3 32 кГц): звук и микрофон одновременно, живой разговор, перебивания;
  music — обычный Bluetooth (A2DP LDAC): лучший звук для музыки, микрофон — только на время реплики (HFP).
Переключение — предпочтительный канал устройства (PreferredBearer) + переподключение.
"""
import asyncio
import json
import logging
import time

log = logging.getLogger("voice-in")

FAIL_MARK = "Failure in Bluetooth audio transport"


async def run(*argv, input_text=None, timeout=20):
    p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE if input_text else None,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(input_text.encode() if input_text else None), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()  # не оставлять зомби (аудит Fable, D21)
        return -1, "timeout"
    return p.returncode, out.decode("utf-8", "replace")


async def find_mac(config_mac="auto", name_hint=""):
    """Адрес наушников: из настроек, иначе сопряжённое устройство со службой Audio Sink — сначала то, чьё имя
    содержит name_hint («JBL Tour One»): первое попавшееся могло оказаться колонкой (аудит Fable, D17)."""
    if config_mac and config_mac != "auto":
        return config_mac
    rc, out = await run("bluetoothctl", "devices", "Paired")
    audio = []
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) >= 2 and parts[0] == "Device":
            rc, info = await run("bluetoothctl", "info", parts[1], timeout=5)
            if "Audio Sink" in info or "Published Audio Capabil" in info:
                audio.append((parts[1], parts[2] if len(parts) > 2 else ""))
    hinted = [mac for mac, name in audio if name_hint and name_hint.lower() in name.lower()]
    return (hinted or [mac for mac, _ in audio] or [None])[0]


async def info(mac):
    rc, out = await run("bluetoothctl", "info", mac, timeout=5)
    st = {"connected": False, "le": False, "bredr": False}
    for line in out.splitlines():
        s = line.strip()
        if s == "Connected: yes":
            st["connected"] = True
        elif s == "LE.Connected: yes":
            st["le"] = True
        elif s == "BREDR.Connected: yes":
            st["bredr"] = True
    return st


async def node_states():
    """Состояния узлов PipeWire наушников: {имя: idle|suspended|running|error}."""
    rc, out = await run("pw-dump", timeout=10)
    try:
        objs = json.loads(out)
    except ValueError:
        return {}
    res = {}
    for o in objs:
        i = o.get("info") or {}
        name = (i.get("props") or {}).get("node.name", "")
        if name.startswith(("bluez_input.", "bluez_output.")):
            res[name] = i.get("state")
    return res


async def card_profile(mac):
    card = "bluez_card." + mac.replace(":", "_")
    rc, out = await run("pactl", "list", "cards")
    cur = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Name: "):
            cur = s[6:]
        elif cur == card and s.startswith("Active Profile: "):
            return s[len("Active Profile: "):]
    return None


async def _failures_since(t0):
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))
    rc, out = await run("journalctl", "--user", "-o", "cat", "-t", "wireplumber", "--since", since, "--no-pager")
    return out.count(FAIL_MARK)


async def probe(mac):
    """Канал LE Audio жив? Полсекунды тишины в наушники и запись с микрофона: если транспорт не поднимается,
    WirePlumber пишет «Failure in Bluetooth audio transport», а узлы уходят в error."""
    t0 = time.time() - 1
    nodes = await node_states()
    sink = next((n for n in nodes if n.startswith("bluez_output.")), None)
    src = next((n for n in nodes if n.startswith("bluez_input.")), None)
    if not sink:
        return False, "нет выхода наушников"
    procs = [await asyncio.create_subprocess_exec(
        "pacat", "--playback", "-d", sink, "--raw", "--rate=16000", "--channels=1", "--format=s16le",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)]
    procs[0].stdin.write(b"\x00\x00" * 16000)
    procs[0].stdin.close()
    if src:
        procs.append(await asyncio.create_subprocess_exec(
            "timeout", "1.5", "parec", "-d", src, "--raw", "--rate=16000", "--channels=1", "--format=s16le",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL))
    await asyncio.sleep(2.0)
    for p in procs:
        if p.returncode is None:
            p.kill()
        await p.wait()
    states = await node_states()
    if any(v == "error" for v in states.values()):
        return False, f"узлы в ошибке: {states}"
    if await _failures_since(t0):
        return False, "сбой транспорта Bluetooth"
    return True, "ок"


async def _wait_connected(mac, want_profile=None, timeout=15):
    # по часам, а не по числу попыток: если bluetoothd не вернулся, каждый вызов bluetoothctl ждал свои 20 с,
    # и «15 секунд» растягивались на 10 минут (аудит Fable, D14)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = await info(mac)
        prof = await card_profile(mac) if st["connected"] else None
        if st["connected"] and prof and prof != "off" and (want_profile is None or prof.startswith(want_profile)):
            return True
        await asyncio.sleep(0.5)
    return False


async def reconnect(mac):
    await run("bluetoothctl", "disconnect", mac, timeout=15)
    await asyncio.sleep(3)
    await run("bluetoothctl", "connect", mac, timeout=30)
    return await _wait_connected(mac)


async def restart_bluetooth(mac):
    await run("sudo", "-n", "systemctl", "restart", "bluetooth", timeout=30)
    await asyncio.sleep(5)
    await run("bluetoothctl", "connect", mac, timeout=30)
    return await _wait_connected(mac)


async def try_connect(mac):
    """Подключить наушники: включить адаптер, если выключен (2026-10-10 после аварийной перезагрузки KDE запомнил
    «Bluetooth выключен»), и подключить; классический канал без ключа (br-connection-key-missing) — через LE Audio,
    ради которого наушники и заведены. True — подключены."""
    rc, show = await run("bluetoothctl", "show", timeout=5)
    if "Powered: no" in show:
        log.warning("Bluetooth был выключен — включаю")
        await run("bluetoothctl", "power", "on", timeout=10)
        await asyncio.sleep(2)
    rc, out = await run("bluetoothctl", "connect", mac, timeout=30)
    if "key-missing" in out or "Failed" in out:
        log.warning("Наушники: обычное подключение не прошло (%s) — пробую LE Audio", out.strip()[-80:])
        await set_bearer(mac, "le")
        rc, out = await run("bluetoothctl", "connect", mac, timeout=30)
    ok = await _wait_connected(mac, timeout=10)
    log.info("Наушники: подключение %s", "удалось" if ok else "не удалось")
    return ok


async def set_bearer(mac, bearer):
    """Предпочтительный канал устройства: le | bredr | last-used."""
    return await run("bluetoothctl", input_text=f"bearer {mac} {bearer}\nquit\n", timeout=10)


class Headset:
    """Следит за наушниками и чинит канал звука сам. Одновременно — одно восстановление."""

    def __init__(self, config, say=None):
        self.config = config
        self.say = say  # async (text) -> None: сказать голосом Ксении (служебно)
        self.mac = None
        self.lock = asyncio.Lock()
        self.last = {"time": None, "result": None}
        self._last_fail = 0.0

    async def ensure_mac(self):
        if not self.mac:
            self.mac = await find_mac(self.config.get("headset_mac", "auto"), self.config.get("headset_name", ""))
        return self.mac

    async def status(self):
        mac = await self.ensure_mac()
        if not mac:
            return {"mac": None, "connected": False}
        st = await info(mac)
        prof = await card_profile(mac) if st["connected"] else None
        mode = "talk" if prof and prof.startswith("bap") else ("music" if prof and prof.startswith(("a2dp", "headset")) else None)
        return {"mac": mac, **st, "profile": prof, "mode": mode, "last_recovery": self.last}

    async def check_and_recover(self, reason="проверка"):
        """Проверить канал LE Audio; при сбое — переподключить, затем перезапустить Bluetooth."""
        if self.lock.locked():
            return {"ok": False, "busy": True}
        async with self.lock:
            mac = await self.ensure_mac()
            if mac and not (await info(mac))["connected"] and reason in ("запуск слуха", "просьба ядра"):
                await try_connect(mac)  # при запуске и по «почини звук» — подключить самим, а не только сообщить
            if not mac or not (await info(mac))["connected"]:
                return {"ok": False, "error": "наушники не подключены"}
            prof = await card_profile(mac)
            if not (prof or "").startswith("bap"):
                return {"ok": True, "mode": "music", "note": "обычный Bluetooth — проверять LE Audio не нужно"}
            ok, why = await probe(mac)
            if ok:
                return {"ok": True, "mode": "talk"}
            log.warning("Наушники: канал LE Audio не поднялся (%s; %s) — переподключаю", why, reason)
            steps = []
            for name, action in (("переподключение", reconnect), ("перезапуск Bluetooth", restart_bluetooth)):
                steps.append(name)
                if await action(mac):
                    await asyncio.sleep(2)
                    ok, why = await probe(mac)
                    if ok:
                        self.last = {"time": time.strftime("%H:%M:%S"), "result": f"починила: {name}"}
                        log.info("Наушники: канал восстановлен (%s)", name)
                        self._told_fail = False
                        if self.say:
                            await self.say("Наушники переподключила, всё работает.")
                        return {"ok": True, "fixed_by": name}
            self.last = {"time": time.strftime("%H:%M:%S"), "result": "не удалось: " + ", ".join(steps)}
            log.error("Наушники: восстановить канал не удалось (%s)", why)
            self._backoff_until = time.time() + 600  # не перезапускать Bluetooth каждую минуту по кругу
            # по просьбе (инструмент, переключение режима) итог скажет сама Ксения — здесь только при само-починке
            if self.say and reason not in ("просьба ядра", "после переключения", "проверка") \
                    and not getattr(self, "_told_fail", False):
                self._told_fail = True  # сказать один раз, а не при каждой неудаче
                await self.say("Не получается наладить звук в наушниках. Выключи их и включи снова, пожалуйста.")
            return {"ok": False, "error": "канал звука наушников не поднимается",
                    "tried": steps, "advice": "выключить и включить наушники"}

    async def set_mode(self, mode):
        """music — обычный Bluetooth (LDAC), talk — LE Audio (живой разговор)."""
        if mode not in ("music", "talk"):
            return {"ok": False, "error": "режим: music или talk"}
        async with self.lock:
            mac = await self.ensure_mac()
            if not mac:
                return {"ok": False, "error": "наушники не найдены"}
            await set_bearer(mac, "le" if mode == "talk" else "bredr")
            await run("bluetoothctl", "disconnect", mac, timeout=15)
            await asyncio.sleep(3)
            await run("bluetoothctl", "connect", mac, timeout=30)
            ok = await _wait_connected(mac, "bap" if mode == "talk" else "a2dp", timeout=20)
            prof = await card_profile(mac)
        if mode == "talk" and ok:
            res = await self.check_and_recover("после переключения")
            ok = res.get("ok", False)
        return {"ok": ok, "mode": mode, "profile": prof,
                **({} if ok else {"error": "наушники не перешли в этот режим", "advice": "выключить и включить наушники"})}

    async def watch(self):
        """Следить за журналом WirePlumber: сбой транспорта — восстановить (не чаще раза в минуту)."""
        while True:
            try:
                p = await asyncio.create_subprocess_exec(
                    "journalctl", "--user", "-f", "-n", "0", "-o", "cat", "-t", "wireplumber",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                async for raw in p.stdout:
                    if FAIL_MARK in raw.decode("utf-8", "replace") and time.time() - self._last_fail > 60 \
                            and time.time() > getattr(self, "_backoff_until", 0):
                        self._last_fail = time.time()
                        await asyncio.sleep(2)  # пусть WirePlumber закончит свою попытку
                        t = asyncio.create_task(self.check_and_recover("сбой транспорта в журнале"))
                        self._tasks = getattr(self, "_tasks", set())
                        self._tasks.add(t)
                        t.add_done_callback(self._tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("слежение за наушниками: %r", e)
            await asyncio.sleep(5)
