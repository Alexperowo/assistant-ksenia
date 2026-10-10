#!/usr/bin/env python3
"""Обход замедления YouTube только для Ксении — без VPN для всего компьютера.

Решение Александра 2026-10-09: VPN на весь компьютер мешает другим программам (Claude Code), поэтому
YouTube — через локальный прокси только для Ксении:
  byedpi (ciadpi, SOCKS5 127.0.0.1:10801) — обходит фильтрацию трафика по имени сайта (DPI), трафик идёт
      напрямую, без чужих серверов; настройка подобрана замером 2026-10-09 (из 7 вариантов открылись и
      youtube.com, и ytimg, и googlevideo ~3 МБ/с);
  мостик HTTP CONNECT -> SOCKS5 (127.0.0.1:10802) — проигрыватель mpv/ffmpeg понимает только HTTP-прокси.
Слушают только 127.0.0.1. Пользуются им только инструменты Ксении (yt-dlp, mpv для отдельного файла).
"""
import asyncio
import os
import signal
import sys

CIADPI = os.path.expanduser("~/.local/opt/byedpi/ciadpi-x86_64")
SOCKS_PORT, HTTP_PORT = 10801, 10802
STRATEGY = "-d1 -s1+s -d3+s -s6+s -d9+s -s12+s -d15+s -s20+s -d25+s -s30+s -d35+s -r1+s -S -a1".split()


async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def socks_connect(host, port):
    r, w = await asyncio.open_connection("127.0.0.1", SOCKS_PORT)
    w.write(b"\x05\x01\x00")
    await w.drain()
    if (await r.readexactly(2))[1] != 0:
        raise ConnectionError("socks auth")
    hb = host.encode("idna")
    w.write(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + port.to_bytes(2, "big"))
    await w.drain()
    head = await r.readexactly(4)
    if head[1] != 0:
        raise ConnectionError(f"socks connect {head[1]}")
    atyp = head[3]
    skip = 4 if atyp == 1 else 16 if atyp == 4 else (await r.readexactly(1))[0]
    await r.readexactly(skip + 2)
    return r, w


def allowed(host: str, port: str) -> bool:
    """Мостик — только для YouTube и подобного: публичные адреса, порты 443/80. Раньше через него любая
    программа компьютера доходила до 127.0.0.1:18100 (мозг) или роутера (аудит Fable, C21)."""
    import ipaddress
    if port not in ("443", "80"):
        return False
    h = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_global and not ip.is_multicast
    except ValueError:
        return bool(h) and "." in h and h != "localhost" and not h.endswith((".local", ".lan", ".home", ".internal",
                                                                             ".localhost", ".localdomain"))


async def handle(cr, cw):
    try:
        line = (await asyncio.wait_for(cr.readline(), 15)).decode("latin-1")
        while (await asyncio.wait_for(cr.readline(), 15)).strip():
            pass  # остальные заголовки CONNECT не нужны
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT" or ":" not in parts[1]:
            cw.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        host, port = parts[1].rsplit(":", 1)
        host = host.strip("[]")
        if not allowed(host, port):
            cw.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        sr, sw = await socks_connect(host, int(port))
        cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await cw.drain()
        await asyncio.gather(pipe(cr, sw), pipe(sr, cw))
    except Exception:
        try:
            cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            cw.close()
        except Exception:
            pass


async def main():
    proc = await asyncio.create_subprocess_exec(CIADPI, "-i", "127.0.0.1", "-p", str(SOCKS_PORT), *STRATEGY,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    server = await asyncio.start_server(handle, "127.0.0.1", HTTP_PORT)
    print(f"unblock: SOCKS5 127.0.0.1:{SOCKS_PORT} (byedpi), HTTP CONNECT 127.0.0.1:{HTTP_PORT}", flush=True)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    waiter = asyncio.create_task(proc.wait())
    await asyncio.wait({waiter, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
    server.close()
    if proc.returncode is None:
        proc.terminate()
        await proc.wait()
    sys.exit(0 if stop.is_set() else 1)  # byedpi упал — systemd перезапустит


if __name__ == "__main__":
    asyncio.run(main())
