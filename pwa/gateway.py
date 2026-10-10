"""Шлюз планшета (PWA) Ксении: HTTPS в домашней сети, вход по PIN, долгая сессия, проксирование к ядру и слуху.

Отдельный сервис ksenia-pwa. Ядро и слух по-прежнему принимают только программы этого компьютера (local_only):
шлюз ходит к ним сам по 127.0.0.1, а планшет видит только шлюз.

  https://<компьютер>:18140/  — приложение (index.html, app.js, …)
  http://<компьютер>:18141/   — только инструкция и корневой сертификат: пока сертификат не установлен,
                                 HTTPS на планшете показывает ошибку, а скачать его нужно до этого.
API (HTTPS; всё, кроме входа, — только с сессией):
  GET  /api/session             — вошёл ли планшет
  POST /api/login {"pin"}       — вход: долгая HttpOnly-сессия (ограничение попыток)
  POST /api/pin/speak           — Ксения произносит код у компьютера (не чаще раза в 30 с)
  POST /api/logout
  GET  /api/ws                  — события разговора и звук ответов (PCM 44,1 кГц)
  GET  /api/live                — живой разговор: звук микрофона планшета потоком, ответы — на планшет
  POST /api/look                — фото с камеры планшета: «что передо мной?» (ответ — голосом на планшете)
  POST /api/utterance           — WAV 16 кГц моно → voice-in /transcribe → реплика в ядро, ответ — на планшет
  POST /api/text {"text"}       — реплика текстом (кнопки «Да»/«Нет»)
  POST /api/stop                — замолчать
  POST /api/listening {"on"}    — планшет слушает: приглушить музыку у компьютера
  GET  /api/control/state       — центр управления: всё состояние Ксении (ядро /control/state)
  POST /api/control/act         — действие центра управления (ядро /control/act)

Компьютер: http://127.0.0.1:18142/ — то же приложение для этого же компьютера (ярлык «Ксения — управление»):
только с 127.0.0.1, без кода входа (программы этого компьютера и так могут обратиться к ядру напрямую);
localhost — безопасный источник для браузера, микрофон работает и без сертификата.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import ssl
import sys
import time
import urllib.parse

import aiohttp
from aiohttp import web

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
COOKIE = "ksenia_session"
PUBLIC_API = {"/api/session", "/api/login", "/api/pin/speak"}
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/styles.css": ("styles.css", "text/css"), "/sw.js": ("sw.js", "text/javascript"),
          "/recorder-worklet.js": ("recorder-worklet.js", "text/javascript"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/control.js": ("control.js", "text/javascript"),
          "/icon-192x192.png": ("icon-192x192.png", "image/png"), "/icon-512x512.png": ("icon-512x512.png", "image/png"),
          "/icon.svg": ("icon.svg", "image/svg+xml")}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "media-src 'self' blob:; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pwa")


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["data_dir"] = os.path.normpath(os.path.join(ROOT, cfg.get("data_dir", "../data/pwa")))
    return cfg


# ---------- доступ: только домашняя сеть ----------

def is_lan(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return (a.is_private or a.is_loopback or a.is_link_local) and not a.is_unspecified and not a.is_multicast


def client_ip(request) -> str:
    """Адрес собеседника по сокету (заголовкам X-Forwarded-* не верим: прокси перед шлюзом нет)."""
    peer = request.transport.get_extra_info("peername") if request.transport else None
    return peer[0] if peer else ""


def hostname(host_header: str):
    try:
        return urllib.parse.urlsplit("//" + (host_header or "")).hostname
    except ValueError:
        return None


# ---------- PIN и сессии ----------

class PinGuard:
    """Ограничение попыток PIN: не больше per_ip ошибок за window с одного адреса и global_limit со всех
    (перебор с разных адресов). Сравнение — за постоянное время."""

    def __init__(self, pin, per_ip=5, window=600, global_limit=20, global_window=3600, clock=time.monotonic):
        self.pin, self.per_ip, self.window = pin, per_ip, window
        self.global_limit, self.global_window, self.clock = global_limit, global_window, clock
        self.fails, self.all_fails = {}, []

    def check(self, ip: str, supplied: str):
        """-> (вошёл, секунд до следующей попытки)."""
        now = self.clock()
        self.all_fails = [t for t in self.all_fails if now - t < self.global_window]
        mine = [t for t in self.fails.get(ip, []) if now - t < self.window]
        self.fails[ip] = mine
        if len(mine) >= self.per_ip:
            return False, int(self.window - (now - mine[0])) + 1
        if len(self.all_fails) >= self.global_limit and mine:
            # общий предел — только для адресов, которые уже ошибались: иначе перебор с чужого устройства на час
            # запирал и Александра с верным кодом (проверка Fable, agent_e/h1_auth). Новый адрес — одна попытка
            return False, int(self.global_window - (now - self.all_fails[0])) + 1
        if hmac.compare_digest(self.pin.encode(), "".join(str(supplied or "").split()).encode()):
            self.fails.pop(ip, None)
            return True, 0
        mine.append(now)
        self.all_fails.append(now)
        return False, 0


def _write_private(path, data: str):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


class Sessions:
    """Долгие сессии планшета. На диске — только SHA-256 от токена: утёкший файл не даёт войти."""

    def __init__(self, path, days=180, clock=time.time):
        self.path, self.ttl, self.clock = path, days * 86400, clock
        try:
            with open(path, encoding="utf-8") as f:
                self.items = json.load(f)
            if not isinstance(self.items, dict):
                self.items = {}
        except (OSError, ValueError):
            self.items = {}
        self._saved = self.clock()

    @staticmethod
    def _h(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _save(self):
        _write_private(self.path, json.dumps(self.items, ensure_ascii=False, indent=1))
        self._saved = self.clock()

    def create(self, agent="") -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self.items[self._h(token)] = {"created": now, "seen": now, "agent": agent[:120]}
        self.items = {k: v for k, v in self.items.items() if now - v.get("seen", 0) < self.ttl}
        self._save()
        return token

    def valid(self, token) -> bool:
        if not token:
            return False
        rec = self.items.get(self._h(token))
        now = self.clock()
        if not rec or now - rec.get("seen", 0) > self.ttl:
            return False
        rec["seen"] = now
        if now - self._saved > 3600:  # «последний раз видели» — на диск не чаще раза в час
            self._save()
        return True

    def revoke(self, token):
        if self.items.pop(self._h(token or ""), None) is not None:
            self._save()


def load_pin(path) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            pin = f.read().strip()
        if pin.isdigit() and len(pin) >= 6:
            return pin
    except OSError:
        pass
    pin = f"{secrets.randbelow(10 ** 6):06d}"
    _write_private(path, pin + "\n")
    log.info("Создан новый код входа для планшета: %s (его же Ксения говорит вслух по кнопке на планшете)", path)
    return pin


# ---------- раздача событий браузерам ----------

class Fanout:
    """События и звук от ядра — всем открытым вкладкам планшета, у каждой своя очередь (порядок сохраняется)."""

    def __init__(self):
        self.queues = {}

    def emit(self, event):
        self._put(("json", event))

    def emit_raw(self, kind, payload):
        self._put((kind, payload))

    def _put(self, item):
        for q in list(self.queues.values()):
            if q.qsize() < 2000:
                q.put_nowait(item)

    async def serve(self, ws):
        q = asyncio.Queue()
        self.queues[ws] = q
        try:
            while True:
                kind, payload = await q.get()
                if kind == "json":
                    await ws.send_json(payload)
                elif kind == "text":
                    await ws.send_str(payload)
                else:
                    await ws.send_bytes(payload)
        except (ConnectionResetError, RuntimeError, aiohttp.ClientError):
            pass
        finally:
            self.queues.pop(ws, None)


class Gateway:
    def __init__(self, cfg):
        self.cfg = cfg
        data = cfg["data_dir"]
        self.pins = PinGuard(load_pin(os.path.join(data, "pin")), per_ip=cfg.get("pin_attempts", 5),
                             window=cfg.get("pin_window_s", 600), global_limit=cfg.get("pin_global_attempts", 20))
        self.sessions = Sessions(os.path.join(data, "sessions.json"), days=cfg.get("session_days", 180))
        self.hosts = {"localhost", "127.0.0.1", "::1"}
        try:
            with open(os.path.join(data, "hosts.json"), encoding="utf-8") as f:
                self.hosts |= {str(h).lower() for h in json.load(f)}
        except (OSError, ValueError):
            log.warning("Нет %s — запустите pwa/make-certs.sh", os.path.join(data, "hosts.json"))
        self.fanout = Fanout()
        self.pending_confirm = None  # вопрос «Отправить?» для вкладки, открытой уже после него
        self.live_end = asyncio.Event()  # ядро закончило живой разговор с планшета
        self.ws_tokens = {}  # открытые соединения планшета -> токен входа (после «Выйти» — закрыть)
        self.core_up = False
        self.session = None
        self.tasks = set()
        self.last_pin_speak = 0.0

    # ----- защита -----

    def is_local_site(self, request):
        """Запрос к сайту для этого компьютера (127.0.0.1:local_port) и с этого же компьютера."""
        sock = request.transport.get_extra_info("sockname") if request.transport else None
        try:
            loop = ipaddress.ip_address(client_ip(request)).is_loopback
        except ValueError:
            loop = False
        return bool(sock) and sock[1] == self.cfg.get("local_port", 18142) and loop

    def host_ok(self, request):
        h = hostname(request.host)
        return bool(h) and h.lower() in self.hosts

    @web.middleware
    async def guard(self, request, handler):
        ip = client_ip(request)
        if not is_lan(ip):
            log.warning("Отклонён запрос не из домашней сети: %s %s", ip, request.path)
            return web.json_response({"error": "только домашняя сеть"}, status=403)
        if not self.host_ok(request):
            # чужое имя в Host — признак DNS rebinding (сертификат на такое имя не выдан)
            return web.json_response({"error": "неизвестный адрес"}, status=421)
        local = self.is_local_site(request)
        unsafe = request.method not in ("GET", "HEAD") or request.headers.get("Upgrade", "").lower() == "websocket"
        if unsafe and request.headers.get("Origin") != f"{'http' if local else 'https'}://{request.host}":
            log.warning("Отклонён запрос с чужим Origin: %s %s", request.headers.get("Origin"), request.path)
            return web.json_response({"error": "чужой источник"}, status=403)
        if request.path.startswith("/api/") and request.path not in PUBLIC_API and not local and \
                not self.sessions.valid(request.cookies.get(COOKIE)):
            return web.json_response({"error": "нужен вход"}, status=401)
        resp = await handler(request)
        if not resp.prepared:
            resp.headers.setdefault("Content-Security-Policy", CSP)
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
            resp.headers.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
            if request.path.startswith("/api/"):
                resp.headers["Cache-Control"] = "no-store"
        return resp

    # ----- связь с ядром -----

    def spawn(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def set_core(self, up):
        if up != self.core_up:
            self.core_up = up
            self.fanout.emit({"type": "link", "core": up})
            if not up and self.pending_confirm:
                # ядро упало — его вопрос «Отправить?» больше никто не решит; кнопки «Да/Нет» убрать
                self.pending_confirm = None
                self.fanout.emit({"type": "confirm_clear", "reason": "core_down"})

    async def core_link(self):
        """Держать WebSocket к ядру и пересылать его события планшету; при разрыве — «нет связи с компьютером»."""
        delay = 1
        while True:
            try:
                async with self.session.ws_connect(self.cfg["core_url"] + "/client", heartbeat=20) as ws:
                    self.set_core(True)
                    delay = 1
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._track(msg.data)
                            self.fanout.emit_raw("text", msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            self.fanout.emit_raw("bytes", msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("ядро недоступно: %r", e)
            self.set_core(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)

    def _track(self, raw):
        try:
            ev = json.loads(raw)
        except ValueError:
            return
        if ev.get("type") == "confirm":
            self.pending_confirm = ev
        elif ev.get("type") == "confirm_clear":
            self.pending_confirm = None
        elif ev.get("type") == "hello":
            self.pending_confirm = ev.get("confirm")
        elif ev.get("type") == "live_end":
            self.live_end.set()

    async def core_post(self, path, body, timeout=30):
        async with self.session.post(self.cfg["core_url"] + path, json=body,
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return r.status, await r.json(content_type=None)

    async def say(self, text, speaker=None):
        """Реплика в ядро; ответ звучит на планшете (output=client). Ждём весь ход — поэтому в фоне.
        speaker — чей голос (отпечаток из слуха): без него «да» мог сказать любой голос рядом с планшетом."""
        body = {"text": text, "output": "client"}
        if isinstance(speaker, dict):
            body["speaker"] = {**speaker, "source": "tablet"}
        try:
            status, res = await self.core_post("/say", body, timeout=600)
            if status != 200:
                self.fanout.emit({"type": "error", "text": "Компьютер не принял реплику."})
        except Exception as e:
            log.warning("ядро не ответило на реплику: %r", e)
            self.fanout.emit({"type": "error", "text": "Нет связи с компьютером."})

    # ----- обработчики -----

    async def h_static(self, request):
        name, ctype = STATIC[request.path]
        resp = web.FileResponse(os.path.join(WEB, name), headers={"Content-Type": ctype})
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    async def h_session(self, request):
        return web.json_response({"authorized": self.is_local_site(request) or
                                  self.sessions.valid(request.cookies.get(COOKIE)),
                                  "core": self.core_up, "local": self.is_local_site(request)})

    async def h_control_state(self, request):
        try:
            async with self.session.get(self.cfg["core_url"] + "/control/state",
                                        timeout=aiohttp.ClientTimeout(total=30)) as r:
                return web.json_response(await r.json(content_type=None), status=r.status)
        except Exception:
            return web.json_response({"error": "Компьютер не ответил."}, status=502)

    async def h_control_act(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return web.json_response({"ok": False, "say": "Непонятный запрос."}, status=400)
        try:
            status, res = await self.core_post("/control/act", body, timeout=120)
            return web.json_response(res, status=status)
        except Exception:
            return web.json_response({"ok": False, "say": "Компьютер не ответил."}, status=502)

    async def h_login(self, request):
        try:
            pin = str((await request.json()).get("pin", ""))
        except (ValueError, AttributeError):
            pin = ""
        ok, wait = self.pins.check(client_ip(request), pin)
        if not ok:
            if wait:
                return web.json_response({"error": f"Слишком много попыток. Подождите {wait // 60 + 1} мин."},
                                         status=429, headers={"Retry-After": str(wait)})
            return web.json_response({"error": "Неверный код."}, status=403)
        token = self.sessions.create(request.headers.get("User-Agent", ""))
        resp = web.json_response({"ok": True})
        resp.set_cookie(COOKIE, token, max_age=self.cfg.get("session_days", 180) * 86400, path="/",
                        secure=True, httponly=True, samesite="Strict")
        log.info("Планшет вошёл: %s", client_ip(request))
        return resp

    async def h_pin_speak(self, request):
        now = time.monotonic()
        if now - self.last_pin_speak < 30:
            return web.json_response({"error": "Код уже звучал, подождите полминуты."}, status=429)
        self.last_pin_speak = now
        digits = ", ".join(self.pins.pin)
        try:
            await self.core_post("/notice", {"text": f"Код для планшета: {digits}."}, timeout=10)
        except Exception:
            return web.json_response({"error": "Компьютер не ответил."}, status=502)
        return web.json_response({"ok": True})

    async def h_logout(self, request):
        token = request.cookies.get(COOKIE)
        self.sessions.revoke(token)
        # уже открытые соединения этого входа — закрыть: иначе после «Выйти» они продолжали получать разговор
        for ws, t in list(self.ws_tokens.items()):
            if t and t == token and not ws.closed:
                self.spawn(ws.close())
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def h_ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
        await ws.prepare(request)
        self.ws_tokens[ws] = request.cookies.get(COOKIE)
        await ws.send_json({"type": "link", "core": self.core_up})
        if self.pending_confirm:
            await ws.send_json(self.pending_confirm)
        sender = asyncio.get_running_loop().create_task(self.fanout.serve(ws))
        try:
            async for msg in ws:  # планшет присылает только «ping» для проверки связи
                if msg.type == aiohttp.WSMsgType.TEXT and msg.data == "ping":
                    self.fanout.emit_raw("text", json.dumps({"type": "pong"}))
        finally:
            sender.cancel()
            self.fanout.queues.pop(ws, None)
            self.ws_tokens.pop(ws, None)
        return ws

    async def h_live(self, request):
        """Живой разговор с планшета: звук микрофона (PCM s16le 16 кГц) — слуху (/push), ядро ведёт живой разговор
        и отвечает на планшет. Эхо собственного голоса Ксении гасит браузер планшета (echoCancellation)."""
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        push = None
        try:
            push = await self.session.ws_connect(self.cfg["voice_in_url"].replace("http", "ws", 1) + "/push", heartbeat=20)
            self.live_end.clear()
            self.spawn(self.core_post("/talk", {"source": "push"}, timeout=30))
            await ws.send_json({"type": "live", "on": True})
            # слух закрыл /push (перезапуск) или ядро закончило разговор — раньше планшет об этом не узнавал и
            # продолжал слать звук в пустоту (проверка Fable, agent_e/h3_live)
            push_gone = asyncio.ensure_future(push.receive())
            core_done = asyncio.ensure_future(self.live_end.wait())
            why = None
            try:
                while True:
                    tablet = asyncio.ensure_future(ws.receive())
                    done, _ = await asyncio.wait({tablet, push_gone, core_done}, return_when=asyncio.FIRST_COMPLETED)
                    if push_gone in done:
                        tablet.cancel()
                        why = "Слух перезапустился — включи живой разговор ещё раз."
                        break
                    if core_done in done:
                        tablet.cancel()
                        why = ""
                        break
                    msg = tablet.result()
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        await push.send_bytes(msg.data)
                    elif (msg.type == aiohttp.WSMsgType.TEXT and msg.data == "stop") or \
                            msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                push_gone.cancel()
                core_done.cancel()
            if why is not None and not ws.closed:
                await ws.send_json({"type": "live", "on": False, **({"error": why} if why else {})})
        except Exception as e:
            log.warning("живой разговор с планшета: %r", e)
            if not ws.closed:
                await ws.send_json({"type": "live", "on": False, "error": "Слух не отвечает."})
        finally:
            if push is not None:
                await push.close()
            try:
                await self.core_post("/stop", {}, timeout=10)
            except Exception:
                pass
            if not ws.closed:
                await ws.close()
        return ws

    async def h_look(self, request):
        """Фото с камеры планшета (JPEG) -> ядро /look; ответ Ксения скажет на планшете."""
        data = await request.read()
        if len(data) < 1000 or not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
            return web.json_response({"error": "нужно фото"}, status=415)
        q = (request.query.get("q") or "")[:200]
        try:
            async with self.session.post(self.cfg["core_url"] + "/look", data=data, params={"q": q} if q else None,
                                         timeout=aiohttp.ClientTimeout(total=120)) as r:
                return web.json_response(await r.json(content_type=None), status=r.status)
        except Exception:
            return web.json_response({"error": "Компьютер не ответил."}, status=502)

    async def h_utterance(self, request):
        data = await request.read()
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return web.json_response({"error": "нужен WAV"}, status=415)
        try:
            async with self.session.post(self.cfg["voice_in_url"] + "/transcribe", data=data,
                                         headers={"Content-Type": "audio/wav"},
                                         timeout=aiohttp.ClientTimeout(total=60)) as r:
                res = await r.json(content_type=None)
                if r.status != 200 or not isinstance(res, dict):
                    raise ValueError(f"voice-in {r.status}")
        except Exception as e:
            log.warning("слух не ответил: %r", e)
            return web.json_response({"error": "Слух не отвечает."}, status=502)
        text = " ".join(str(res.get("text") or "").split())
        self.fanout.emit({"type": "heard", "text": text})
        if text:
            self.spawn(self.say(text, res.get("speaker")))
        return web.json_response({"text": text})

    async def h_text(self, request):
        try:
            text = " ".join(str((await request.json()).get("text") or "").split())[:2000]
        except (ValueError, AttributeError):
            text = ""
        if not text:
            return web.json_response({"error": "нужен text"}, status=400)
        self.spawn(self.say(text))
        return web.json_response({"ok": True})

    async def h_stop(self, request):
        self.live_end.set()  # «Стоп» на планшете — и живой поток звука закончить, а не слать его в пустоту
        try:
            await self.core_post("/stop", {}, timeout=10)
        except Exception:
            return web.json_response({"error": "Компьютер не ответил."}, status=502)
        return web.json_response({"ok": True})

    async def h_listening(self, request):
        try:
            on = bool((await request.json()).get("on"))
            await self.core_post("/duck", {"on": on}, timeout=5)
        except Exception:
            return web.json_response({"ok": False})
        return web.json_response({"ok": True})

    # ----- сборка -----

    def app(self):
        app = web.Application(middlewares=[self.guard], client_max_size=12 * 1024 * 1024)
        app.add_routes([web.get(p, self.h_static) for p in STATIC] + [
            web.get("/api/session", self.h_session), web.post("/api/login", self.h_login),
            web.post("/api/pin/speak", self.h_pin_speak), web.post("/api/logout", self.h_logout),
            web.get("/api/ws", self.h_ws), web.get("/api/live", self.h_live), web.post("/api/look", self.h_look),
            web.post("/api/utterance", self.h_utterance),
            web.post("/api/text", self.h_text), web.post("/api/stop", self.h_stop),
            web.post("/api/listening", self.h_listening),
            web.get("/api/control/state", self.h_control_state), web.post("/api/control/act", self.h_control_act)])
        app.on_startup.append(self._start)
        app.on_cleanup.append(self._stop)
        return app

    async def _start(self, app):
        self.session = aiohttp.ClientSession()
        self.spawn(self.core_link())

    async def _stop(self, app):
        for t in list(self.tasks):
            t.cancel()
        await self.session.close()

    def plain_app(self):
        """HTTP: только инструкция и корневой сертификат (его надо скачать до того, как HTTPS заработает)."""
        @web.middleware
        async def lan_only(request, handler):
            if not is_lan(client_ip(request)):
                return web.Response(status=403, text="только домашняя сеть")
            return await handler(request)

        async def help_page(request):
            h = hostname(request.host)
            h = h if h and h.lower() in self.hosts else next(iter(sorted(self.hosts - {"localhost", "127.0.0.1", "::1"})), "localhost")
            url = f"https://{'[' + h + ']' if ':' in h else h}:{self.cfg.get('https_port', 18140)}/"
            with open(os.path.join(WEB, "cert.html"), encoding="utf-8") as f:
                page = f.read().replace("{{https}}", url)
            return web.Response(text=page, content_type="text/html",
                                headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

        async def ca(request):
            path = os.path.join(self.cfg["data_dir"], "ca.crt")
            if not os.path.exists(path):
                return web.Response(status=404, text="сертификат ещё не создан: pwa/make-certs.sh")
            return web.FileResponse(path, headers={"Content-Type": "application/x-x509-ca-cert",
                                                   "Content-Disposition": 'attachment; filename="ksenia-ca.crt"'})

        app = web.Application(middlewares=[lan_only])
        app.add_routes([web.get("/", help_page), web.get("/ksenia-ca.crt", ca)])
        return app


def tls_context(data_dir):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(os.path.join(data_dir, "server.crt"), os.path.join(data_dir, "server.key"))
    return ctx


async def serve(cfg):
    gw = Gateway(cfg)
    try:
        ctx = tls_context(cfg["data_dir"])
    except (OSError, ssl.SSLError) as e:
        log.error("Нет сертификата (%s). Создайте его: pwa/make-certs.sh", e)
        sys.exit(1)
    runners = []
    for app, port, tls in ((gw.app(), cfg.get("https_port", 18140), ctx), (gw.plain_app(), cfg.get("http_port", 18141), None)):
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, cfg.get("host", "0.0.0.0"), port, ssl_context=tls).start()
        if tls is not None:
            # то же приложение для этого компьютера: только 127.0.0.1, без TLS (localhost — безопасный источник)
            await web.TCPSite(runner, "127.0.0.1", cfg.get("local_port", 18142)).start()
        runners.append(runner)
    log.info("Шлюз планшета: https://…:%s (приложение), http://…:%s (сертификат)",
             cfg.get("https_port", 18140), cfg.get("http_port", 18141))
    try:
        await asyncio.Event().wait()
    finally:
        for r in runners:
            await r.cleanup()


def main():
    asyncio.run(serve(load_config()))


if __name__ == "__main__":
    main()
