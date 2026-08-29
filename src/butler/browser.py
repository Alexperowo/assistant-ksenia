from __future__ import annotations

import atexit
import ipaddress
import json
import os
import queue
import socket
import subprocess
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from butler.config import Settings
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception


class BrowserError(RuntimeError):
    pass


FINANCIAL_MARKERS = (
    "оплатить",
    "оплата",
    "купить",
    "оформить заказ",
    "подтвердить заказ",
    "банковский перевод",
    "перевести деньги",
    "place order",
    "checkout",
    "purchase",
    "pay now",
    "confirm order",
    "bank transfer",
    "send money",
)


def contains_financial_action(*values: object) -> bool:
    normalized = " ".join(
        re.findall(r"[\w-]+", " ".join(str(value or "") for value in values).casefold())
    )
    return any(marker in normalized for marker in FINANCIAL_MARKERS)


def public_http_url(value: object) -> bool:
    """Allow only HTTP(S) destinations whose current addresses are all public."""
    raw = str(value or "")
    if not raw or "\\" in raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        return False
    try:
        parsed = urlparse(raw)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return False

    # inet_aton also understands legacy IPv4 spellings such as 127.1,
    # 2130706433 and hexadecimal/octal forms that urlparse treats as names.
    try:
        legacy_ipv4 = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(hostname)))
    except OSError:
        legacy_ipv4 = None
    if legacy_ipv4 is not None:
        return legacy_ipv4.is_global

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        return address.is_global

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_hostname.split(".")
    if (
        len(ascii_hostname) > 253
        or any(not label or len(label) > 63 for label in labels)
        or any(label.startswith("-") or label.endswith("-") for label in labels)
    ):
        return False
    try:
        resolved = socket.getaddrinfo(
            ascii_hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError):
        return False
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for item in resolved:
        try:
            addresses.append(ipaddress.ip_address(str(item[4][0]).split("%", 1)[0]))
        except (IndexError, TypeError, ValueError):
            return False
    return bool(addresses) and all(address.is_global for address in addresses)


class BrowserReader:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        config = settings.raw.get("browser", {})
        self.python = Path(str(config.get("python", "")))
        self.executable = Path(str(config.get("executable", "")))
        self.profile_dir = Path(str(config.get("profile_dir", "")))
        self.headless = bool(config.get("headless", True))
        self.worker = settings.root / "scripts" / "browser_worker.py"
        self.service_worker = settings.root / "scripts" / "browser_service.py"
        self.timeout = int(config.get("timeout_seconds", 45))
        self.max_text = int(config.get("max_text_chars", 6000))
        self.persistent = bool(config.get("persistent", True))
        self.max_parallel = max(1, min(8, int(config.get("max_parallel", 4))))
        self._service: subprocess.Popen[str] | None = None
        self._service_reader: threading.Thread | None = None
        self._service_stderr_reader: threading.Thread | None = None
        self._service_ready = threading.Event()
        self._service_error = ""
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._service_lock = threading.RLock()
        self._write_lock = threading.Lock()
        atexit.register(self.close)

    def _validate(self, mode: str) -> None:
        if not self.python.is_file():
            raise BrowserError(f"Не найден Python браузера: {self.python}")
        if not self.executable.is_file():
            raise BrowserError(f"Не найден Chromium: {self.executable}")
        if not str(self.profile_dir):
            raise BrowserError("Не настроен отдельный профиль браузера Ксении.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if mode not in {"search", "open", "interact"}:
            raise BrowserError(f"Неизвестный режим браузера: {mode}")

    def _environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }

    def _read_once(self, mode: str, value: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [
                    str(self.python), "-u", str(self.worker),
                    "--mode", mode, "--value-stdin",
                    "--executable", str(self.executable),
                    "--profile", str(self.profile_dir),
                    "--headless", "true" if self.headless else "false",
                    "--max-text", str(self.max_text),
                ],
                input=value,
                cwd=str(self.worker.parent.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=self._environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserError("Браузер не ответил вовремя.") from exc
        except OSError as exc:
            raise BrowserError(f"Не удалось запустить браузер: {exc}") from exc
        if result.returncode != 0:
            raise BrowserError((result.stderr or result.stdout).strip() or "Браузер завершился с ошибкой.")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BrowserError("Браузер вернул некорректный ответ.") from exc
        if not isinstance(payload, dict):
            raise BrowserError("Браузер вернул ответ неверного формата.")
        return payload

    def _read_service_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            detail = line.strip()
            if detail:
                diagnostic_event(
                    self.settings,
                    "browser",
                    "service_stderr",
                    level="warning",
                    detail=detail,
                )

    def _read_service_events(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                diagnostic_event(
                    self.settings,
                    "browser",
                    "invalid_service_event",
                    level="warning",
                    detail=line.strip(),
                )
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "ready":
                self._service_ready.set()
                diagnostic_event(
                    self.settings,
                    "browser",
                    "service_ready",
                    worker_pid=process.pid,
                    max_parallel=event.get("max_parallel", 0),
                )
                continue
            request_id = str(event.get("id", ""))
            if not request_id:
                continue
            with self._service_lock:
                destination = self._pending.pop(request_id, None)
            if destination is not None:
                destination.put(event)
        with self._service_lock:
            abandoned = list(self._pending.values())
            self._pending.clear()
            if self._service is process:
                self._service = None
        self._service_error = "Постоянный браузерный сервис завершился."
        self._service_ready.set()
        for destination in abandoned:
            destination.put({"ok": False, "error": self._service_error})
        diagnostic_event(
            self.settings,
            "browser",
            "service_exited",
            level="warning" if abandoned else "info",
            returncode=process.poll(),
            abandoned_count=len(abandoned),
        )

    def _start_service(self) -> bool:
        with self._service_lock:
            if self._service is not None and self._service.poll() is None:
                return True
            if not self.service_worker.is_file():
                return False
            self._service_ready.clear()
            self._service_error = ""
            try:
                process = subprocess.Popen(
                    [
                        str(self.python),
                        "-u",
                        str(self.service_worker),
                        "--executable",
                        str(self.executable),
                        "--profile",
                        str(self.profile_dir),
                        "--headless",
                        "true" if self.headless else "false",
                        "--max-text",
                        str(self.max_text),
                        "--max-parallel",
                        str(self.max_parallel),
                    ],
                    cwd=str(self.service_worker.parent.parent),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=self._environment(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                diagnostic_exception(self.settings, "browser", "service_start_failed", exc)
                return False
            self._service = process
            self._service_reader = threading.Thread(
                target=self._read_service_events, args=(process,), daemon=True
            )
            self._service_stderr_reader = threading.Thread(
                target=self._read_service_stderr, args=(process,), daemon=True
            )
            self._service_reader.start()
            self._service_stderr_reader.start()
        if not self._service_ready.wait(timeout=min(20.0, max(5.0, self.timeout))):
            diagnostic_event(
                self.settings, "browser", "service_ready_timeout", level="warning"
            )
            self.close()
            return False
        if process.poll() is not None:
            return False
        return True

    def _read_persistent(self, mode: str, value: str) -> dict[str, Any] | None:
        if not self._start_service():
            return None
        destination: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        request_id = uuid.uuid4().hex
        with self._service_lock:
            process = self._service
            if process is None or process.stdin is None or process.poll() is not None:
                return None
            self._pending[request_id] = destination
        try:
            with self._write_lock:
                process.stdin.write(
                    json.dumps(
                        {"id": request_id, "mode": mode, "value": value},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            with self._service_lock:
                self._pending.pop(request_id, None)
            self.close()
            return None
        try:
            event = destination.get(timeout=self.timeout + 5)
        except queue.Empty as exc:
            with self._service_lock:
                self._pending.pop(request_id, None)
            raise BrowserError("Браузер не ответил вовремя.") from exc
        if not bool(event.get("ok", False)):
            raise BrowserError(str(event.get("error", "Браузер не выполнил запрос.")))
        payload = event.get("value")
        if not isinstance(payload, dict):
            raise BrowserError("Браузерный сервис вернул ответ неверного формата.")
        return payload

    def read(self, mode: str, value: str) -> dict[str, Any]:
        self._validate(mode)
        destination = value
        if mode == "interact":
            try:
                payload = json.loads(value)
                destination = str(payload.get("url", "")) if isinstance(payload, dict) else ""
            except json.JSONDecodeError:
                destination = ""
        if mode in {"open", "interact"} and not public_http_url(destination):
            raise BrowserError(
                "Браузеру разрешены только публичные http/https адреса. "
                "Локальные файлы, localhost и домашняя сеть запрещены."
            )
        started = time.monotonic()
        diagnostic_event(
            self.settings,
            "browser",
            "request_started",
            mode=mode,
            value=value,
            persistent=bool(getattr(self, "persistent", False)),
        )
        try:
            payload = None
            if mode == "interact" and bool(getattr(self, "persistent", False)):
                payload = self._read_persistent(mode, value)
            if payload is None:
                diagnostic_event(
                    self.settings,
                    "browser",
                    "service_fallback_to_single_request",
                    level="warning",
                    mode=mode,
                )
                payload = self._read_once(mode, value)
        except Exception as exc:
            diagnostic_exception(
                self.settings,
                "browser",
                "request_failed",
                exc,
                mode=mode,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise
        diagnostic_event(
            self.settings,
            "browser",
            "request_completed",
            mode=mode,
            duration_ms=round((time.monotonic() - started) * 1000),
            result_count=(
                len(payload.get("results", []))
                if isinstance(payload.get("results"), list)
                else 0
            ),
            offer_count=(
                len(payload.get("offers", []))
                if isinstance(payload.get("offers"), list)
                else 0
            ),
            response_chars=len(str(payload.get("text", ""))),
            url=payload.get("url", ""),
        )
        return payload

    def close(self) -> None:
        lock = getattr(self, "_service_lock", None)
        if lock is None:
            return
        with lock:
            process = self._service
            self._service = None
            if process is not None and process.stdin is not None and process.poll() is None:
                try:
                    process.stdin.write('{"cmd":"shutdown"}\n')
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
        if process is None:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    @staticmethod
    def _action_risk(url: str, actions: list[dict[str, Any]]) -> str:
        pieces = [url]
        for action in actions:
            if not isinstance(action, dict):
                continue
            pieces.extend(
                str(action.get(key, ""))
                for key in ("type", "selector", "text", "key")
            )
        value = " ".join(pieces).casefold()
        normalized = " ".join(re.findall(r"[\w-]+", value))
        if contains_financial_action(normalized):
            return "financial"
        send_markers = (
            "отправить", "send", "submit message", "publish", "опубликовать",
            "compose-send", "mail-send", "message-send",
        )
        message_hosts = (
            "mail.", "gmail.", "outlook.", "web.telegram.", "web.whatsapp.",
            "discord.", "slack.", "vk.com/im", "messages.",
        )
        pressed_enter = any(
            str(action.get("type", "")) == "press"
            and str(action.get("key", "")).casefold() == "enter"
            for action in actions if isinstance(action, dict)
        )
        if any(marker in normalized for marker in send_markers):
            return "send_message"
        if pressed_enter and any(host in url.casefold() for host in message_hosts):
            return "send_message"
        return "normal"

    def interact(
        self,
        url: str,
        actions: list[dict[str, Any]],
        *,
        allow_send: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(actions, list) or not 1 <= len(actions) <= 10:
            raise BrowserError("Нужно указать от одного до десяти действий браузера.")
        risk = self._action_risk(url, actions)
        if risk == "financial":
            raise BrowserError("Покупки и платежи запрещены через браузерный инструмент.")
        if risk == "send_message" and not allow_send:
            raise BrowserError(
                "Отправка сообщения требует отдельного инструмента и отдельного подтверждения."
            )
        payload = json.dumps({"url": url, "actions": actions}, ensure_ascii=False)
        return self.read("interact", payload)

    def send_message(self, url: str, actions: list[dict[str, Any]]) -> dict[str, Any]:
        risk = self._action_risk(url, actions)
        if risk != "send_message":
            raise BrowserError(
                "В действиях не найдена явная команда отправки. Сначала подготовьте текст, "
                "затем укажите кнопку «Отправить» в этом отдельном действии."
            )
        return self.interact(url, actions, allow_send=True)
