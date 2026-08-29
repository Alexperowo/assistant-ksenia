from __future__ import annotations

import hmac
import hashlib
import ipaddress
import json
import queue
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from butler.approval import approval_explanation
from butler.atomic_io import atomic_write_text, exclusive_file_lock
from butler.chat import ChatError
from butler.confirmation import confirmation_text
from butler.config import Settings
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception
from butler.model_manager import ModelManagerError
from butler.orchestrator import RoutedAgentSession
from butler.speech import SpeechAnnouncer
from butler.tasking import (
    DurableTaskStore,
    TaskCancelled,
    TaskControl,
    TaskState,
)
from butler.user_messages import spoken_agent_error


MAX_STORED_TASKS = 100


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LanTask:
    id: str
    message: str
    status: str = "В очереди"
    answer: str = ""
    error: str = ""
    events: list[dict[str, str]] = field(default_factory=list)
    confirmation: dict[str, Any] | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    revision: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "message": self.message,
            "status": self.status,
            "answer": self.answer,
            "error": self.error,
            "events": list(self.events),
            "confirmation": dict(self.confirmation) if self.confirmation else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "done": self.status in {"Готово", "Ошибка", "Отменено"},
        }


class LanTaskStore:
    def __init__(self, journal: DurableTaskStore | None = None) -> None:
        self._tasks: dict[str, LanTask] = {}
        self._lock = threading.Lock()
        self.journal = journal

    def create(self, message: str) -> LanTask:
        journal_task = (
            self.journal.create(message, channel="lan") if self.journal else None
        )
        task = LanTask(
            id=journal_task.id if journal_task else uuid.uuid4().hex,
            message=message,
        )
        task.events.append({"status": task.status, "at": task.created_at})
        with self._lock:
            excess = len(self._tasks) - MAX_STORED_TASKS + 1
            if excess > 0:
                completed = [
                    task_id
                    for task_id, existing in self._tasks.items()
                    if existing.status in {"Готово", "Ошибка", "Отменено"}
                ]
                for task_id in completed[:excess]:
                    self._tasks.pop(task_id, None)
            self._tasks[task.id] = task
        return task

    @staticmethod
    def _journal_state(status: str) -> TaskState:
        normalized = status.casefold()
        if status == "Готово":
            return TaskState.COMPLETED
        if status == "Ошибка":
            return TaskState.FAILED
        if status == "Отменено":
            return TaskState.CANCELLED
        if status == "Приостановлено":
            return TaskState.PAUSED
        if "подтвержден" in normalized:
            return TaskState.WAITING_CONFIRMATION
        if "план" in normalized:
            return TaskState.PLANNING
        return TaskState.RUNNING

    def update(
        self,
        task_id: str,
        status: str,
        *,
        answer: str = "",
        error: str = "",
        expected_states: set[TaskState] | None = None,
    ) -> dict[str, Any]:
        journal_snapshot: dict[str, Any] | None = None
        target_state = self._journal_state(status)
        if self.journal is not None:
            try:
                journal_snapshot = self.journal.transition(
                    task_id,
                    target_state,
                    status,
                    answer=answer,
                    error=error,
                    confirmation=None if status != "Ожидаю подтверждение" else ...,
                    resumable=status not in {"Готово", "Ошибка", "Отменено"},
                    expected_states=expected_states,
                )
            except ValueError as exc:
                current = self.journal.get(task_id)
                if current is not None and current.get("state") == TaskState.CANCELLED:
                    raise TaskCancelled("Задача уже отменена Александром.") from exc
                raise
        with self._lock:
            task = self._tasks[task_id]
            if task.status in {"Готово", "Ошибка", "Отменено"} and status != task.status:
                if task.status == "Отменено":
                    raise TaskCancelled("Задача уже отменена Александром.")
                raise ValueError("Завершённую LAN-задачу нельзя запустить снова.")
            task.status = status
            task.updated_at = _now()
            if not task.events or task.events[-1]["status"] != status:
                task.events.append({"status": status, "at": task.updated_at})
            if answer:
                task.answer = answer
            if error:
                task.error = error
            if journal_snapshot is not None:
                task.revision = int(journal_snapshot.get("revision", task.revision))
            snapshot = task.snapshot()
        return snapshot

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.snapshot() if task is not None else None

    def request_confirmation(
        self, task_id: str, name: str, arguments: dict[str, Any], message: str
    ) -> dict[str, Any]:
        confirmation_id = uuid.uuid4().hex
        digest_payload = json.dumps(
            {"tool": name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(digest_payload).hexdigest()
        revision = 1
        journal_snapshot: dict[str, Any] | None = None
        if self.journal is not None:
            current = self.journal.get(task_id)
            if current is None:
                raise KeyError(task_id)
            expected_revision = int(current.get("revision", 0))
            revision = expected_revision + 1
            durable_confirmation = {
                "tool": name,
                "message": message,
                "confirmation_id": confirmation_id,
                "revision": revision,
                "digest": digest,
            }
            try:
                journal_snapshot = self.journal.transition(
                    task_id,
                    TaskState.WAITING_CONFIRMATION,
                    "Ожидаю подтверждение",
                    confirmation=durable_confirmation,
                    expected_revision=expected_revision,
                    expected_states={
                        TaskState.PLANNING,
                        TaskState.RUNNING,
                        TaskState.VERIFYING,
                    },
                )
            except ValueError as exc:
                latest = self.journal.get(task_id)
                if latest is not None and latest.get("state") == TaskState.CANCELLED:
                    raise TaskCancelled("Задача отменена до подтверждения.") from exc
                raise
        public_confirmation = {
            "tool": name,
            "arguments": {},
            "message": message,
            "confirmation_id": confirmation_id,
            "revision": revision,
            "digest": digest,
        }
        with self._lock:
            task = self._tasks[task_id]
            if task.status in {"Готово", "Ошибка", "Отменено"}:
                raise TaskCancelled("Задача завершена до подтверждения.")
            task.status = "Ожидаю подтверждение"
            task.updated_at = _now()
            task.events.append({"status": task.status, "at": task.updated_at})
            task.confirmation = public_confirmation
            if journal_snapshot is not None:
                task.revision = int(journal_snapshot.get("revision", revision))
            snapshot = task.snapshot()
        return snapshot

    def clear_confirmation(self, task_id: str) -> dict[str, Any]:
        journal_snapshot: dict[str, Any] | None = None
        if self.journal is not None:
            try:
                journal_snapshot = self.journal.transition(
                    task_id,
                    TaskState.RUNNING,
                    "Продолжаю",
                    confirmation=None,
                    expected_states={TaskState.WAITING_CONFIRMATION},
                )
            except ValueError as exc:
                current = self.journal.get(task_id)
                if current is not None and current.get("state") == TaskState.CANCELLED:
                    raise TaskCancelled("Задача отменена во время подтверждения.") from exc
                raise
        with self._lock:
            task = self._tasks[task_id]
            if task.status == "Отменено":
                raise TaskCancelled("Задача отменена во время подтверждения.")
            task.confirmation = None
            task.status = "Продолжаю"
            task.updated_at = _now()
            if journal_snapshot is not None:
                task.revision = int(journal_snapshot.get("revision", task.revision))
            return task.snapshot()


class LanApplication:
    def __init__(
        self,
        settings: Settings,
        speech: SpeechAnnouncer,
        pin: str,
    ) -> None:
        self.settings = settings
        self.speech = speech
        self.pin = pin
        self.task_journal = DurableTaskStore(settings.runtime_dir)
        self.store = LanTaskStore(self.task_journal)
        self._queue: queue.Queue[str] = queue.Queue()
        self._session = RoutedAgentSession(settings)
        self._confirmation_lock = threading.Lock()
        self._confirmations: dict[str, dict[str, Any]] = {}
        self._auth_lock = threading.Lock()
        self._auth_failures: dict[str, list[float]] = {}
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()
        diagnostic_event(self.settings, "lan", "application_ready")

    def authorized(self, supplied_pin: str, client: str = "local") -> bool:
        now = time.monotonic()
        with self._auth_lock:
            recent = [stamp for stamp in self._auth_failures.get(client, []) if now - stamp < 60]
            if len(recent) >= 10:
                self._auth_failures[client] = recent
                diagnostic_event(
                    getattr(self, "settings", None),
                    "lan",
                    "auth_rate_limited",
                    level="warning",
                    client=client,
                    recent_failure_count=len(recent),
                )
                return False
            allowed = hmac.compare_digest(self.pin, supplied_pin.strip())
            if allowed:
                self._auth_failures.pop(client, None)
            else:
                recent.append(now)
                self._auth_failures[client] = recent
            diagnostic_event(
                getattr(self, "settings", None),
                "lan",
                "auth_succeeded" if allowed else "auth_failed",
                level="info" if allowed else "warning",
                client=client,
                recent_failure_count=0 if allowed else len(recent),
            )
            return allowed

    def submit(self, message: str) -> dict[str, Any]:
        task = self.store.create(message)
        self._queue.put(task.id)
        diagnostic_event(
            self.settings,
            "lan",
            "task_submitted",
            task_id=task.id,
            message=message,
            queue_size=self._queue.qsize(),
        )
        return task.snapshot()

    def decide(
        self,
        task_id: str,
        approved: bool,
        confirmation_id: str,
        revision: int,
        digest: str,
    ) -> bool:
        with self._confirmation_lock:
            pending = self._confirmations.get(task_id)
            if pending is None:
                return False
            if (
                not hmac.compare_digest(
                    str(pending["confirmation_id"]), str(confirmation_id)
                )
                or int(pending["revision"]) != revision
                or not hmac.compare_digest(str(pending["digest"]), str(digest))
            ):
                return False
            self._confirmations.pop(task_id, None)
            decision = pending["decision"]
            decision["approved"] = approved
            pending["event"].set()
            diagnostic_event(
                self.settings,
                "lan",
                "confirmation_decided",
                task_id=task_id,
                approved=approved,
            )
            return True

    def control(self, task_id: str, action: str) -> dict[str, Any] | None:
        if self.store.get(task_id) is None:
            return None
        diagnostic_event(
            self.settings, "lan", "task_control", task_id=task_id, action=action
        )
        if action == "pause":
            return self.store.update(
                task_id,
                "Приостановлено",
                expected_states={
                    TaskState.PLANNING,
                    TaskState.RUNNING,
                    TaskState.VERIFYING,
                },
            )
        if action == "resume":
            return self.store.update(
                task_id,
                "Продолжаю",
                expected_states={TaskState.PAUSED},
            )
        if action == "cancel":
            result = self.store.update(task_id, "Отменено")
            with self._confirmation_lock:
                pending = self._confirmations.pop(task_id, None)
                if pending is not None:
                    pending["decision"]["cancelled"] = True
                    pending["event"].set()
            return result
        raise ValueError("Неизвестная команда управления задачей.")

    def _confirm(
        self,
        task_id: str,
        name: str,
        arguments: dict[str, Any],
        message: str,
    ) -> bool:
        waiter = threading.Event()
        decision: dict[str, bool] = {}
        prompt = f"{confirmation_text(name, arguments)} {approval_explanation(name)}"
        with self._confirmation_lock:
            snapshot = self.store.request_confirmation(
                task_id, name, arguments, prompt
            )
            confirmation = snapshot.get("confirmation") or {}
            pending: dict[str, Any] = {
                "event": waiter,
                "decision": decision,
                "confirmation_id": str(confirmation.get("confirmation_id", "")),
                "revision": int(confirmation.get("revision", -1)),
                "digest": str(confirmation.get("digest", "")),
            }
            self._confirmations[task_id] = pending
        diagnostic_event(
            self.settings,
            "lan",
            "confirmation_requested",
            task_id=task_id,
            tool_name=name,
            argument_names=sorted(str(key) for key in arguments),
        )
        self.speech.say(prompt + " Откройте локальную панель и выберите да или нет.")
        received = waiter.wait(timeout=300)
        with self._confirmation_lock:
            if self._confirmations.get(task_id) is pending:
                self._confirmations.pop(task_id, None)
        if decision.get("cancelled", False):
            raise TaskCancelled("Задача отменена во время подтверждения.")
        self.store.clear_confirmation(task_id)
        if not received:
            self.store.update(task_id, "Подтверждение не получено")
            diagnostic_event(
                self.settings,
                "lan",
                "confirmation_timeout",
                level="warning",
                task_id=task_id,
                tool_name=name,
            )
            return False
        return bool(decision.get("approved", False))

    def _announce(self, task_id: str, status: str) -> None:
        self.store.update(task_id, status)
        self.speech.say(status)

    def _work(self) -> None:
        while True:
            task_id = self._queue.get()
            task = self.store.get(task_id)
            if task is None:
                self._queue.task_done()
                continue
            try:
                started = time.monotonic()
                diagnostic_event(
                    self.settings, "lan", "task_started", task_id=task_id
                )
                control = TaskControl(self.task_journal, task_id)
                self.store.update(
                    task_id,
                    "Начинаю",
                    expected_states={TaskState.QUEUED, TaskState.INTERRUPTED},
                )
                reply = self._session.ask(
                    task["message"],
                    on_status=lambda status, current=task_id: self._announce(current, status),
                    on_confirmation=lambda name, arguments, message, current=task_id: self._confirm(
                        current, name, arguments, message
                    ),
                    max_steps=self.settings.developer_max_steps,
                    control=control,
                )
                self.store.update(task_id, "Готово", answer=reply.text)
                diagnostic_event(
                    self.settings,
                    "lan",
                    "task_completed",
                    task_id=task_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    answer=reply.text,
                )
                self.speech.say(reply.text)
            except TaskCancelled:
                diagnostic_event(
                    self.settings,
                    "lan",
                    "task_cancelled",
                    task_id=task_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                self.store.update(task_id, "Отменено")
                self.speech.say("Задача отменена.")
            except (ChatError, ModelManagerError, OSError, KeyError) as exc:
                diagnostic_exception(
                    self.settings,
                    "lan",
                    "task_failed",
                    exc,
                    task_id=task_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                self.store.update(task_id, "Ошибка", error=str(exc))
                self.speech.say(spoken_agent_error(exc))
            finally:
                self._queue.task_done()


class ButlerLanServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: LanApplication) -> None:
        super().__init__(address, ButlerLanHandler)
        self.app = app


class ButlerLanHandler(BaseHTTPRequestHandler):
    server: ButlerLanServer

    def _send_json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _send_page(self) -> None:
        page = self.server.app.settings.root / "web" / "index.html"
        try:
            payload = page.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Интерфейс не найден"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > 32768:
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _authorized(self) -> bool:
        return self.server.app.authorized(
            self.headers.get("X-Butler-Pin", ""), self.client_address[0]
        )

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_page()
            return
        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "assistant": self.server.app.settings.assistant_name},
            )
            return
        if path.startswith("/api/tasks/"):
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Неверный PIN"})
                return
            task_id = path.rsplit("/", 1)[-1]
            task = self.server.app.store.get(task_id)
            if task is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Задача не найдена"})
            else:
                self._send_json(HTTPStatus.OK, task)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Не найдено"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/auth":
            value = self._read_json()
            supplied = str((value or {}).get("pin", ""))
            if self.server.app.authorized(supplied, self.client_address[0]):
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Неверный PIN"})
            return
        if path == "/api/tasks":
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Неверный PIN"})
                return
            value = self._read_json()
            message = str((value or {}).get("message", "")).strip()
            if not message or len(message) > 12000:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Введите сообщение"})
                return
            self._send_json(HTTPStatus.ACCEPTED, self.server.app.submit(message))
            return
        if path.startswith("/api/tasks/") and path.endswith("/confirmation"):
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Неверный PIN"})
                return
            parts = path.strip("/").split("/")
            task_id = parts[2] if len(parts) == 4 else ""
            value = self._read_json()
            approved = (value or {}).get("approved")
            confirmation_id = (value or {}).get("confirmation_id")
            revision = (value or {}).get("revision")
            digest = (value or {}).get("digest")
            if (
                not isinstance(approved, bool)
                or not isinstance(confirmation_id, str)
                or type(revision) is not int
                or not isinstance(digest, str)
            ):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Нужно решение"})
                return
            if self.server.app.decide(
                task_id, approved, confirmation_id, revision, digest
            ):
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._send_json(HTTPStatus.CONFLICT, {"error": "Подтверждение уже не ожидается"})
            return
        if path.startswith("/api/tasks/") and path.endswith("/control"):
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Неверный PIN"})
                return
            parts = path.strip("/").split("/")
            task_id = parts[2] if len(parts) == 4 else ""
            value = self._read_json()
            action = str((value or {}).get("action", ""))
            if action not in {"pause", "resume", "cancel"}:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Неверная команда"})
                return
            try:
                result = self.server.app.control(task_id, action)
            except ValueError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if result is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Задача не найдена"})
            else:
                self._send_json(HTTPStatus.OK, result)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Не найдено"})

    def log_message(self, format: str, *args: object) -> None:
        diagnostic_event(
            self.server.app.settings,
            "lan_http",
            "request",
            client=self.client_address[0],
            method=self.command,
            path=urlparse(self.path).path,
            http_status=str(args[1]) if len(args) > 1 else "",
        )


def _address_priority(value: str) -> tuple[int, str]:
    """Prefer ordinary home-LAN ranges over VPN/container ranges."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return (99, value)
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_private:
        return (99, value)
    if value.startswith("192.168."):
        return (0, value)
    if value.startswith("10."):
        return (1, value)
    if address in ipaddress.ip_network("172.16.0.0/12"):
        return (2, value)
    return (3, value)


def local_network_addresses(port: int) -> list[str]:
    try:
        raw = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        raw = []
    addresses = sorted(
        {
            value
            for value in raw
            if _address_priority(value)[0] < 99
            and not ipaddress.ip_address(value).is_loopback
            and not ipaddress.ip_address(value).is_link_local
        },
        key=_address_priority,
    )
    if not addresses:
        addresses = ["127.0.0.1"]
    return [f"http://{address}:{port}" for address in addresses]


def persistent_pin(settings: Settings) -> str:
    path = settings.runtime_dir / "lan" / "pin.txt"
    with exclusive_file_lock(path):
        try:
            value = path.read_text(encoding="ascii").strip()
            if value.isdigit() and len(value) == 6:
                return value
        except OSError:
            pass
        value = str(secrets.randbelow(900000) + 100000)
        atomic_write_text(path, value, encoding="ascii", mode=0o600)
        return value


def run_lan_server(
    settings: Settings,
    speech: SpeechAnnouncer,
    *,
    host: str = "auto",
    port: int = 8765,
    pin: str | None = None,
) -> None:
    access_pin = pin or persistent_pin(settings)
    app = LanApplication(settings, speech, access_pin)
    discovered = local_network_addresses(port)
    if host == "auto":
        addresses = discovered[:1]
        address = addresses[0]
        bind_host = urlparse(address).hostname or "127.0.0.1"
    elif host in {"0.0.0.0", "::"}:
        addresses = discovered
        address = addresses[0]
        bind_host = host
    else:
        bind_host = host
        addresses = [f"http://{host}:{port}"]
        address = addresses[0]
    server = ButlerLanServer((bind_host, port), app)
    diagnostic_event(
        settings,
        "lan",
        "server_started",
        bind_host=bind_host,
        port=port,
        url=address,
        address_count=len(addresses),
    )
    print("\n=== Ксения в локальной сети ===")
    print(f"Адрес: {address}")
    for alternative in addresses[1:]:
        print(f"Запасной адрес: {alternative}")
    print(f"PIN: {access_pin}")
    print(f"Интерфейс: {bind_host}")
    print("Для остановки закройте окно или нажмите Ctrl+C.\n")
    spoken_address = (
        address.removeprefix("http://")
        .replace(".", " точка ")
        .replace(":", " порт ")
    )
    speech.say(
        f"Локальная панель готова. Адрес: {spoken_address}. "
        f"Пин код: {', '.join(access_pin)}"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        diagnostic_event(settings, "lan", "server_stopped", bind_host=bind_host, port=port)
