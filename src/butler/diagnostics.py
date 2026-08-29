from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from butler.atomic_io import exclusive_file_lock
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


_LOCK = threading.RLock()
_SEQUENCE = itertools.count(1)
_PROCESS_STARTED = time.monotonic()
_SESSION_ID = os.environ.get("BUTLER_SESSION_ID", "").strip() or uuid.uuid4().hex[:16]
_TRACE_FIELDS: ContextVar[dict[str, str]] = ContextVar(
    "butler_diagnostic_trace_fields", default={}
)
_TRACE_FIELD_NAMES = frozenset({"run_id", "trace_id", "turn_id", "task_id"})
_SENSITIVE_KEYS = {
    "answer",
    "authorization",
    "body",
    "command",
    "content",
    "cookie",
    "headers",
    "message",
    "new_text",
    "old_text",
    "password",
    "pin",
    "prompt",
    "query",
    "request",
    "response",
    "secret",
    "text",
    "token",
    "transcript",
    "value",
}
_SAFE_METADATA_SUFFIXES = (
    "_bytes",
    "_chars",
    "_count",
    "_duration_ms",
    "_length",
    "_ms",
    "_sha256",
    "_tokens",
)
_SAFE_METADATA_KEYS = {
    "command_name",
    "request_id",
    "run_id",
    "task_id",
    "trace_id",
    "turn_id",
    "worker_pid",
}
_RESERVED_EVENT_KEYS = {
    "component",
    "event",
    "level",
    "pid",
    "monotonic_ns",
    "process_uptime_ms",
    "sequence",
    "session_id",
    "thread",
    "time",
}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[-_ ]?key|authorization|cookie|password|pin|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL = re.compile(r"https?://[^\s<>\"']+")


def session_id() -> str:
    return _SESSION_ID


def new_trace_id() -> str:
    """Return an opaque local correlation id without user content."""
    return uuid.uuid4().hex


def current_trace_fields() -> dict[str, str]:
    """Return a detached snapshot suitable for an async request record."""
    return dict(_TRACE_FIELDS.get())


@contextmanager
def trace_scope(**fields: object) -> Iterator[dict[str, str]]:
    """Attach stable correlation fields to diagnostics in the current context.

    Context variables intentionally do not leak into unrelated threads. Services
    with long-lived reader threads capture ``current_trace_fields()`` when they
    enqueue a request and restore the snapshot while handling its result.
    """
    inherited = current_trace_fields()
    for key, value in fields.items():
        normalized = str(key).casefold()
        if normalized not in _TRACE_FIELD_NAMES or value in (None, ""):
            continue
        inherited[normalized] = str(value)
    token = _TRACE_FIELDS.set(inherited)
    try:
        yield dict(inherited)
    finally:
        _TRACE_FIELDS.reset(token)


def bind_trace_context(callback: Callable[..., Any]) -> Callable[..., Any]:
    """Bind the current correlation snapshot to a future thread callback."""
    fields = current_trace_fields()

    def traced(*args: object, **kwargs: object) -> Any:
        with trace_scope(**fields):
            return callback(*args, **kwargs)

    return traced


def enabled(source: object) -> bool:
    try:
        return _options(source)[1]
    except Exception:
        return False


def text_metadata(value: object) -> dict[str, object]:
    text = "" if value is None else str(value)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return {"redacted": True, "length": len(text), "sha256": digest}


def safe_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return "<invalid-url>"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "<redacted-url>"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _scrub_string(value: object, *, limit: int = 4000) -> str:
    text = str(value or "")
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", text)
    text = _URL.sub(lambda match: safe_url(match.group(0)), text)
    if len(text) > limit:
        return text[:limit] + "…<truncated>"
    return text


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized in _SAFE_METADATA_KEYS:
        return False
    if normalized.endswith(_SAFE_METADATA_SUFFIXES):
        return False
    if normalized in _SENSITIVE_KEYS:
        return True
    pieces = {piece for piece in normalized.split("_") if piece}
    return bool(pieces.intersection(_SENSITIVE_KEYS))


def safe_value(value: Any, key: str = "") -> Any:
    normalized_key = key.casefold().replace("-", "_")
    if _is_sensitive_key(normalized_key):
        return text_metadata(value)
    if normalized_key in {"url", "uri", "target_url", "source_url"}:
        return safe_url(value)
    if normalized_key in {"error", "detail", "reason", "stack", "traceback"}:
        return _scrub_string(value, limit=8000)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {
            str(item_key): safe_value(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [safe_value(item) for item in value]
    if isinstance(value, str):
        return _scrub_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _scrub_string(value)


def _options(source: object) -> tuple[Path, bool, int, int]:
    if isinstance(source, Path):
        runtime_dir = source
        config: dict[str, object] = {}
        missing_runtime = False
    else:
        runtime_value = getattr(source, "runtime_dir", None)
        root_value = getattr(source, "root", None)
        missing_runtime = runtime_value is None and root_value is None
        runtime_dir = Path(runtime_value or (Path(root_value) / "runtime" if root_value else Path.cwd() / "runtime"))
        raw = getattr(source, "raw", {})
        config = raw.get("diagnostics", {}) if isinstance(raw, dict) else {}
        if not isinstance(config, dict):
            config = {}
    running_tests = "unittest" in sys.modules or os.environ.get(
        "BUTLER_DIAGNOSTICS_DISABLED", ""
    ).strip() == "1"
    enabled = (
        bool(config.get("enabled", True))
        and not missing_runtime
        and (not running_tests or bool(config.get("allow_during_tests", False)))
    )
    max_bytes = int(config.get("max_file_bytes", 0) or 0)
    if max_bytes <= 0:
        max_bytes = max(1, int(config.get("max_file_mb", 8))) * 1024 * 1024
    backup_count = max(1, min(int(config.get("backup_count", 6)), 20))
    return runtime_dir, enabled, max_bytes, backup_count


def rotate_file(path: Path, max_bytes: int, backup_count: int, incoming_bytes: int = 0) -> None:
    """Rotate a bounded runtime log. The caller must hold any required lock."""
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        return
    if current_size + max(0, incoming_bytes) <= max_bytes:
        return
    oldest = path.with_name(f"{path.name}.{backup_count}")
    try:
        oldest.unlink()
    except FileNotFoundError:
        pass
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        target = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            source.replace(target)
    path.replace(path.with_name(f"{path.name}.1"))


def event(
    source: object,
    component: str,
    name: str,
    *,
    level: str = "info",
    **fields: object,
) -> Path | None:
    """Append one privacy-safe structured diagnostic event.

    Logging is deliberately best-effort: diagnostics must never stop the assistant.
    Conversation content, commands, credentials, queries and URL parameters are
    replaced by a length and a stable short fingerprint.
    """
    try:
        runtime_dir, enabled, max_bytes, backup_count = _options(source)
        if not enabled:
            return None
        target = runtime_dir / "logs" / "diagnostics.jsonl"
        combined_fields: dict[str, object] = {
            **current_trace_fields(),
            **fields,
        }
        safe_fields = {
            (
                f"reported_{key}"
                if str(key).casefold() in _RESERVED_EVENT_KEYS
                else str(key)
            ): safe_value(value, str(key))
            for key, value in combined_fields.items()
        }
        payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
            "session_id": _SESSION_ID,
            "sequence": next(_SEQUENCE),
            "process_uptime_ms": round((time.monotonic() - _PROCESS_STARTED) * 1000),
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "level": str(level).casefold(),
            "component": str(component),
            "event": str(name),
            **safe_fields,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        with _LOCK, exclusive_file_lock(target, timeout=2.0):
            target.parent.mkdir(parents=True, exist_ok=True)
            rotate_file(target, max_bytes, backup_count, len(encoded))
            with target.open("ab") as log:
                log.write(encoded)
        return target
    except Exception:
        return None


def milestone(
    source: object,
    name: str,
    **fields: object,
) -> Path | None:
    """Record one privacy-safe timestamp in an end-to-end performance trace."""
    return event(source, "performance", name, milestone=True, **fields)


def exception(
    source: object,
    component: str,
    name: str,
    exc: BaseException,
    **fields: object,
) -> Path | None:
    return event(
        source,
        component,
        name,
        level="error",
        error_type=type(exc).__name__,
        error=str(exc),
        stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        **fields,
    )
