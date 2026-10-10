"""Сторож сети для браузера Ксении: Chromium ходит в интернет только через этот прокси на 127.0.0.1.

Зачем: адрес страницы может прийти из чужого текста (страница, сообщение ВК, находка помощника), а проверка
адреса перед goto не спасает:
- перенаправление (302) на 127.0.0.1 или 192.168.x.x Chromium выполняет сам — Playwright route его не видит;
- DNS rebinding: при проверке имя указывает на публичный адрес, при загрузке — уже на локальный;
- скрипты и картинки страницы тоже могут стучаться в локальную сеть (роутер, принтер, сервисы Ксении).
Прокси получает каждое соединение (включая каждый шаг перенаправления), сам находит адрес по имени и соединяется
только с этим проверенным публичным адресом. Локальные имена и адреса — отказ (403).
"""
import asyncio
import ipaddress
import logging
import socket
import urllib.parse

log = logging.getLogger("core")

LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".home.arpa", ".intranet", ".corp",
                  ".localdomain")
HOP_HEADERS = {"proxy-connection", "connection", "keep-alive", "proxy-authorization", "te", "trailer", "upgrade"}
TIMEOUT_S = 15

_server = {"srv": None, "port": None}


_V4_IN_V6 = [ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::ffff:0:0:0/96"),
             ipaddress.ip_network("::/96")]  # NAT64, SIIT, устаревшие «IPv4-совместимые»: внутри — адрес IPv4


def ip_is_public(ip) -> bool:
    ip = ipaddress.ip_address(ip)
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.is_site_local:  # fec0::/10 — устаревшие адреса локальной сети
            return False
        elif any(ip in n for n in _V4_IN_V6):
            # 64:ff9b::7f00:1 — это 127.0.0.1 через NAT64 (аудит Fable, C18)
            ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def name_is_local(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    return not h or h == "localhost" or h.endswith(LOCAL_SUFFIXES) or "." not in h  # «router», «nas» — имена сети


async def resolve_public(host: str, port: int):
    """Публичный адрес для соединения или None. Если имя даёт хоть один локальный адрес — отказ целиком."""
    host = (host or "").strip("[]")
    try:
        return host if ip_is_public(host) else None  # адрес числом
    except ValueError:
        pass
    if name_is_local(host):
        return None
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    ips = [i[4][0] for i in infos]
    if not ips or not all(ip_is_public(ip) for ip in ips):
        return None
    return ips[0]


def _split_host_port(target, default):
    u = urllib.parse.urlsplit("//" + target)
    return u.hostname, u.port or default


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _refuse(writer, host, why="локальный адрес"):
    log.warning("Браузер: отказ в соединении с %s (%s)", host, why)
    try:
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
    finally:
        writer.close()


async def _handle(reader, writer):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT_S)
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, OSError):
        writer.close()
        return
    if method.upper() == "CONNECT":
        host, port = _split_host_port(target, 443)
        first = b""
    else:
        u = urllib.parse.urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            await _refuse(writer, target[:80], "не http")
            return
        host, port = u.hostname, u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        kept = [ln for ln in lines[1:] if ln and ln.split(":", 1)[0].strip().lower() not in HOP_HEADERS]
        # одно соединение — один запрос: иначе Chromium мог бы пустить по нему запрос к другому (непроверенному) хосту
        first = (f"{method} {path} {version}\r\n" + "".join(f"{ln}\r\n" for ln in kept) +
                 "Connection: close\r\n\r\n").encode("latin-1")
    ip = await resolve_public(host, port)
    if not ip:
        await _refuse(writer, f"{host}:{port}")
        return
    try:
        r2, w2 = await asyncio.wait_for(asyncio.open_connection(ip, port), TIMEOUT_S)
    except (OSError, asyncio.TimeoutError):
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        writer.close()
        return
    if first:
        w2.write(first)
        # дальше от браузера — только тело ЭТОГО запроса: по тому же соединению Chromium мог бы послать запрос
        # к другому хосту, и он ушёл бы на уже открытый сервер мимо проверки (аудит Fable, C4)
        length = next((int(ln.split(":", 1)[1]) for ln in lines[1:]
                       if ln.lower().startswith("content-length:") and ln.split(":", 1)[1].strip().isdigit()), 0)
        try:
            if length:
                w2.write(await asyncio.wait_for(reader.readexactly(length), TIMEOUT_S))
            await w2.drain()
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            w2.close()
            writer.close()
            return
        await _pipe(r2, writer)  # ответ (сервер закроет — просили Connection: close), и соединение браузера тоже
        return
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await asyncio.gather(_pipe(reader, w2), _pipe(r2, writer))


async def start() -> int:
    """Запустить прокси (один раз) и вернуть его порт."""
    if _server["srv"] is None:
        srv = await asyncio.start_server(_handle, "127.0.0.1", 0)
        _server["srv"], _server["port"] = srv, srv.sockets[0].getsockname()[1]
    return _server["port"]


def browser_proxy(port: int) -> dict:
    """Настройка для Playwright. «<-loopback>» снимает встроенное исключение Chromium для localhost — иначе
    127.0.0.1 шёл бы мимо прокси."""
    return {"server": f"http://127.0.0.1:{port}", "bypass": "<-loopback>"}
