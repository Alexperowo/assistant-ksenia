from __future__ import annotations

import json
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from butler.config import ModelRequestMode, ModelService, Settings
from butler.diagnostics import current_trace_fields
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import milestone as diagnostic_milestone
from butler.diagnostics import new_trace_id
from butler.local_auth import local_api_key


class ChatError(RuntimeError):
    pass


def _selected_service(settings: object, service: ModelService | None) -> ModelService:
    if service is not None:
        return service
    factory = getattr(settings, "model_service", None)
    if callable(factory):
        return factory("primary")
    # Lightweight transport tests and embedding clients may provide only the
    # long-standing host/port contract.  Keep that protocol compatible.
    return ModelService(
        name="primary",
        host=str(getattr(settings, "host")),
        port=int(getattr(settings, "port")),
        state_file=Path("state.json"),
    )


_READER_LOCK = threading.Lock()
_ACTIVE_READER_THREADS = 0
_CANCELLED_STREAMS = 0
_STUCK_READER_THREADS = 0


class _ReaderObservation:
    def __init__(self, diagnostics_source: object, request_id: str) -> None:
        self.diagnostics_source = diagnostics_source
        self.request_id = request_id
        self.trace_fields = current_trace_fields()
        self.started = time.monotonic()
        self.finished = threading.Event()
        self.cancelled = False
        self.stuck = False


def _reader_counts() -> dict[str, int]:
    with _READER_LOCK:
        return {
            "active_reader_threads": _ACTIVE_READER_THREADS,
            "cancelled_streams": _CANCELLED_STREAMS,
            "stuck_reader_threads": _STUCK_READER_THREADS,
        }


def _reader_started(observation: _ReaderObservation) -> None:
    global _ACTIVE_READER_THREADS
    with _READER_LOCK:
        _ACTIVE_READER_THREADS += 1
        counts = {
            "active_reader_threads": _ACTIVE_READER_THREADS,
            "cancelled_streams": _CANCELLED_STREAMS,
            "stuck_reader_threads": _STUCK_READER_THREADS,
        }
    if observation.diagnostics_source is not None:
        diagnostic_event(
            observation.diagnostics_source,
            "chat",
            "reader_thread_started",
            request_id=observation.request_id,
            **observation.trace_fields,
            **counts,
        )


def _reader_finished(observation: _ReaderObservation) -> None:
    global _ACTIVE_READER_THREADS, _STUCK_READER_THREADS
    with _READER_LOCK:
        _ACTIVE_READER_THREADS = max(0, _ACTIVE_READER_THREADS - 1)
        if observation.stuck:
            _STUCK_READER_THREADS = max(0, _STUCK_READER_THREADS - 1)
        counts = {
            "active_reader_threads": _ACTIVE_READER_THREADS,
            "cancelled_streams": _CANCELLED_STREAMS,
            "stuck_reader_threads": _STUCK_READER_THREADS,
        }
        observation.finished.set()
    if observation.diagnostics_source is not None:
        diagnostic_event(
            observation.diagnostics_source,
            "chat",
            "reader_thread_finished",
            request_id=observation.request_id,
            duration_ms=round((time.monotonic() - observation.started) * 1000),
            cancelled=observation.cancelled,
            **observation.trace_fields,
            **counts,
        )


def _cancel_reader(
    observation: _ReaderObservation,
    response: object,
    *,
    wait_seconds: float = 0.1,
) -> None:
    global _CANCELLED_STREAMS, _STUCK_READER_THREADS
    cancel_started = time.monotonic()
    with _READER_LOCK:
        if not observation.cancelled:
            observation.cancelled = True
            _CANCELLED_STREAMS += 1
    try:
        setattr(response, "_ksenia_reader_close_managed", True)
    except (AttributeError, TypeError):
        pass
    raw = getattr(getattr(response, "fp", None), "raw", None)
    response_socket = getattr(raw, "_sock", None)
    socket_shutdown = False
    if response_socket is not None:
        try:
            response_socket.shutdown(socket.SHUT_RDWR)
            socket_shutdown = True
        except OSError:
            pass
    stopped = observation.finished.wait(max(0.0, wait_seconds))
    with _READER_LOCK:
        if not stopped and observation.finished.is_set():
            stopped = True
        if not stopped and not observation.stuck:
            observation.stuck = True
            _STUCK_READER_THREADS += 1
    close = getattr(response, "close", None)
    if callable(close):
        if stopped:
            try:
                close()
            except OSError:
                pass
        else:
            threading.Thread(target=_close_response, args=(response,), daemon=True).start()
    with _READER_LOCK:
        counts = {
            "active_reader_threads": _ACTIVE_READER_THREADS,
            "cancelled_streams": _CANCELLED_STREAMS,
            "stuck_reader_threads": _STUCK_READER_THREADS,
        }
    if observation.diagnostics_source is not None:
        diagnostic_event(
            observation.diagnostics_source,
            "chat",
            "reader_shutdown_observed",
            level="info" if stopped else "warning",
            request_id=observation.request_id,
            reader_shutdown_latency_ms=round(
                (time.monotonic() - cancel_started) * 1000
            ),
            reader_stopped=stopped,
            socket_shutdown=socket_shutdown,
            **observation.trace_fields,
            **counts,
        )


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except OSError:
        pass


def _close_response_if_owned(response: object | None) -> None:
    if response is None or bool(
        getattr(response, "_ksenia_reader_close_managed", False)
    ):
        return
    _close_response(response)


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _usage_token_metrics(usage: object) -> dict[str, int | float | None]:
    value = usage if isinstance(usage, dict) else {}
    details = value.get("prompt_tokens_details", {})
    details = details if isinstance(details, dict) else {}
    prompt_tokens = _optional_nonnegative_int(value.get("prompt_tokens"))
    completion_tokens = _optional_nonnegative_int(value.get("completion_tokens"))
    total_tokens = _optional_nonnegative_int(value.get("total_tokens"))
    cached_tokens = _optional_nonnegative_int(details.get("cached_tokens"))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_prompt_tokens": cached_tokens,
        "prompt_cache_hit_ratio": (
            round(cached_tokens / prompt_tokens, 6)
            if cached_tokens is not None and prompt_tokens
            else None
        ),
    }


def default_max_tokens(settings: Settings) -> int:
    value = settings.raw.get("generation", {}).get("max_tokens", 4096)
    try:
        return max(64, int(value))
    except (TypeError, ValueError):
        return 4096


def _api_headers(settings: Settings) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {local_api_key(settings)}",
    }


def _message_metrics(messages: list[dict[str, Any]]) -> dict[str, object]:
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    roles: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role", "unknown"))
        roles[role] = roles.get(role, 0) + 1
    return {
        "message_count": len(messages),
        "input_chars": len(serialized),
        "roles": roles,
    }


def normalize_system_messages(
    messages: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep one leading system message for strict community chat templates."""
    system_parts: list[str] = []
    ordinary: list[dict[str, Any]] = []
    for raw in messages:
        message = dict(raw)
        if str(message.get("role", "")) == "system":
            content = message.get("content", "")
            if isinstance(content, str) and content.strip():
                system_parts.append(content.strip())
            elif content not in (None, ""):
                system_parts.append(json.dumps(content, ensure_ascii=False))
            continue
        ordinary.append(message)
    if not system_parts:
        return ordinary
    return [
        {"role": "system", "content": "\n\n".join(system_parts)},
        *ordinary,
    ]


class SentenceChunker:
    """Collect model tokens and release complete phrases for speech."""

    _boundary = re.compile(r"^(.+?[.!?…]+(?:\s+|$))", re.DOTALL)

    def __init__(self, minimum_length: int = 24, maximum_length: int = 280) -> None:
        self.minimum_length = minimum_length
        self.maximum_length = maximum_length
        self.buffer = ""

    def feed(self, text: str) -> list[str]:
        self.buffer += text
        chunks: list[str] = []
        while True:
            match = self._boundary.match(self.buffer)
            if match and match.group(1).strip():
                chunk = match.group(1).strip()
                self.buffer = self.buffer[match.end() :]
                chunks.append(chunk)
                continue
            if len(self.buffer) >= self.maximum_length:
                split_at = self.buffer.rfind(" ", 0, self.maximum_length)
                if split_at < self.minimum_length:
                    split_at = self.maximum_length
                chunks.append(self.buffer[:split_at].strip())
                self.buffer = self.buffer[split_at:].lstrip()
                continue
            break
        return [chunk for chunk in chunks if chunk]

    def finish(self) -> str:
        remaining = self.buffer.strip()
        self.buffer = ""
        return remaining


def _cancellable_response_lines(
    response,
    checkpoint: Callable[[], None],
    *,
    poll_seconds: float = 0.1,
    diagnostics_source: object = None,
    request_id: str = "",
) -> Iterator[bytes]:
    """Read a possibly stalled HTTP stream without delaying cancellation."""

    messages: queue.Queue[tuple[str, object]] = queue.Queue()
    observation = _ReaderObservation(diagnostics_source, request_id)

    def read_response() -> None:
        try:
            for raw_line in response:
                messages.put(("line", raw_line))
        except BaseException as exc:
            messages.put(("error", exc))
        finally:
            messages.put(("done", None))
            _reader_finished(observation)

    _reader_started(observation)
    threading.Thread(target=read_response, daemon=True).start()
    try:
        while True:
            checkpoint()
            try:
                kind, value = messages.get(timeout=poll_seconds)
            except queue.Empty:
                continue
            if kind == "done":
                return
            if kind == "error":
                if isinstance(value, BaseException):
                    raise value
                raise OSError("Чтение ответа модели завершилось неизвестной ошибкой.")
            if not isinstance(value, bytes):
                raise OSError("Сервер модели вернул строку потока неизвестного типа.")
            yield value
    except BaseException:
        _cancel_reader(observation, response)
        raise


def _cancellable_response_read(
    response,
    checkpoint: Callable[[], None],
    *,
    poll_seconds: float = 0.1,
    diagnostics_source: object = None,
    request_id: str = "",
) -> bytes:
    messages: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
    observation = _ReaderObservation(diagnostics_source, request_id)

    def read_response() -> None:
        try:
            messages.put(("value", response.read()))
        except BaseException as exc:
            messages.put(("error", exc))
        finally:
            _reader_finished(observation)

    _reader_started(observation)
    threading.Thread(target=read_response, daemon=True).start()
    try:
        while True:
            checkpoint()
            try:
                kind, value = messages.get(timeout=poll_seconds)
            except queue.Empty:
                continue
            if kind == "error":
                if isinstance(value, BaseException):
                    raise value
                raise OSError("Чтение ответа модели завершилось неизвестной ошибкой.")
            if not isinstance(value, bytes):
                raise OSError("Сервер модели вернул ответ неизвестного типа.")
            return value
    except BaseException:
        _cancel_reader(observation, response)
        raise


def count_chat_tokens(
    settings: Settings,
    messages: Iterable[dict[str, Any]],
    *,
    checkpoint: Callable[[], None] | None = None,
) -> int:
    """Count tokens with the active llama.cpp tokenizer, with a safe fallback."""
    message_list = normalize_system_messages(messages)
    content = json.dumps(message_list, ensure_ascii=False, separators=(",", ":"))
    started = time.monotonic()
    request_id = new_trace_id()
    request = urllib.request.Request(
        f"http://{settings.host}:{settings.port}/tokenize",
        data=json.dumps(
            {"content": content, "add_special": False}, ensure_ascii=False
        ).encode("utf-8"),
        headers=_api_headers(settings),
        method="POST",
    )
    try:
        if checkpoint is not None:
            checkpoint()
        response = urllib.request.urlopen(request, timeout=30)
        try:
            raw = (
                _cancellable_response_read(
                    response,
                    checkpoint,
                    diagnostics_source=settings,
                    request_id=request_id,
                )
                if checkpoint is not None
                else response.read()
            )
            value = json.loads(raw.decode("utf-8", errors="replace"))
        finally:
            _close_response_if_owned(response)
        tokens = value.get("tokens", []) if isinstance(value, dict) else []
        if isinstance(tokens, list):
            diagnostic_event(
                settings,
                "chat",
                "token_count_completed",
                duration_ms=round((time.monotonic() - started) * 1000),
                token_count=len(tokens),
                request_id=request_id,
                **_message_metrics(message_list),
            )
            return len(tokens)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        diagnostic_event(
            settings,
            "chat",
            "token_count_fallback",
            level="warning",
            duration_ms=round((time.monotonic() - started) * 1000),
            error_type=type(exc).__name__,
            request_id=request_id,
            **_message_metrics(message_list),
        )
    fallback = max(1, len(content) // 2)
    return fallback


def stream_chat(
    settings: Settings,
    messages: Iterable[dict[str, Any]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    checkpoint: Callable[[], None] | None = None,
    service: ModelService | None = None,
    request_mode: ModelRequestMode | None = None,
) -> Iterator[str]:
    message_list = normalize_system_messages(messages)
    selected_max_tokens = (
        request_mode.max_tokens
        if max_tokens is None and request_mode is not None
        else default_max_tokens(settings) if max_tokens is None else max_tokens
    )
    selected_temperature = (
        request_mode.temperature
        if temperature is None and request_mode is not None
        else 0.3 if temperature is None else temperature
    )
    endpoint = _selected_service(settings, service)
    url = f"http://{endpoint.host}:{endpoint.port}/v1/chat/completions"
    payload: dict[str, Any] = {
        "messages": message_list,
        "stream": True,
        "stream_options": {"include_usage": True},
        "cache_prompt": True,
        "temperature": selected_temperature,
        "max_tokens": selected_max_tokens,
    }
    if request_mode is not None:
        payload["chat_template_kwargs"] = {
            "enable_thinking": request_mode.enable_thinking
        }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers=_api_headers(settings),
        method="POST",
    )
    started = time.monotonic()
    request_id = new_trace_id()
    first_token_ms: int | None = None
    output_chars = 0
    completed = False
    usage: dict[str, Any] = {}
    diagnostic_event(
        settings,
        "chat",
        "stream_started",
        request_id=request_id,
        max_tokens=selected_max_tokens,
        temperature=selected_temperature,
        service=endpoint.name,
        request_mode=request_mode.name if request_mode is not None else "default",
        **_message_metrics(message_list),
    )
    diagnostic_milestone(
        settings,
        "llm_request_start",
        request_id=request_id,
        streaming=True,
    )
    try:
        if checkpoint is not None:
            checkpoint()
        response = urllib.request.urlopen(request, timeout=600)
        try:
            lines = (
                _cancellable_response_lines(
                    response,
                    checkpoint,
                    diagnostics_source=settings,
                    request_id=request_id,
                )
                if checkpoint is not None
                else iter(response)
            )
            try:
                for raw_line in lines:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                        if isinstance(event.get("usage"), dict):
                            usage = event["usage"]
                        choices = event.get("choices", [])
                        if not isinstance(choices, list) or not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        continue
                    if content:
                        if first_token_ms is None:
                            first_token_ms = round(
                                (time.monotonic() - started) * 1000
                            )
                            diagnostic_milestone(
                                settings,
                                "llm_first_token",
                                request_id=request_id,
                                first_token_ms=first_token_ms,
                            )
                        output_chars += len(str(content))
                        yield str(content)
            except BaseException:
                close_lines = getattr(lines, "close", None)
                if callable(close_lines):
                    close_lines()
                raise
        finally:
            _close_response_if_owned(response)
        if checkpoint is not None:
            checkpoint()
        completed = True
    except urllib.error.HTTPError as exc:
        diagnostic_event(
            settings,
            "chat",
            "stream_http_error",
            level="error",
            http_status=exc.code,
            duration_ms=round((time.monotonic() - started) * 1000),
            request_id=request_id,
        )
        detail = exc.read().decode("utf-8", errors="replace")
        raise ChatError(f"Сервер модели вернул ошибку {exc.code}: {detail}") from exc
    except (OSError, urllib.error.URLError) as exc:
        diagnostic_event(
            settings,
            "chat",
            "stream_connection_error",
            level="error",
            error_type=type(exc).__name__,
            duration_ms=round((time.monotonic() - started) * 1000),
            request_id=request_id,
        )
        raise ChatError(f"Не удалось связаться с локальной моделью: {exc}") from exc
    except Exception as exc:
        if type(exc).__name__ in {"TaskCancelled", "LiveInterrupted"}:
            diagnostic_milestone(
                settings,
                "llm_actually_cancelled",
                request_id=request_id,
                error_type=type(exc).__name__,
            )
        raise
    finally:
        token_metrics = _usage_token_metrics(usage)
        diagnostic_event(
            settings,
            "chat",
            "stream_completed" if completed else "stream_closed",
            level="info" if completed else "warning",
            duration_ms=round((time.monotonic() - started) * 1000),
            first_token_ms=first_token_ms,
            output_chars=output_chars,
            request_id=request_id,
            usage_available=bool(usage),
            **token_metrics,
        )
        diagnostic_milestone(
            settings,
            "llm_generation_end",
            request_id=request_id,
            outcome="completed" if completed else "closed",
        )


def complete_chat(
    settings: Settings,
    messages: Iterable[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    checkpoint: Callable[[], None] | None = None,
    on_content_delta: Callable[[str], None] | None = None,
    service: ModelService | None = None,
    request_mode: ModelRequestMode | None = None,
) -> dict[str, Any]:
    """Request one complete response, optionally exposing safe tools to the model."""
    endpoint = _selected_service(settings, service)
    url = f"http://{endpoint.host}:{endpoint.port}/v1/chat/completions"
    message_list = normalize_system_messages(messages)
    selected_max_tokens = (
        request_mode.max_tokens
        if max_tokens is None and request_mode is not None
        else default_max_tokens(settings) if max_tokens is None else max_tokens
    )
    selected_temperature = (
        request_mode.temperature
        if temperature is None and request_mode is not None
        else 0.2 if temperature is None else temperature
    )
    streaming = checkpoint is not None or on_content_delta is not None
    payload: dict[str, Any] = {
        "messages": message_list,
        "stream": streaming,
        "cache_prompt": True,
        "temperature": selected_temperature,
        "max_tokens": selected_max_tokens,
    }
    if streaming:
        payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if request_mode is not None:
        payload["chat_template_kwargs"] = {
            "enable_thinking": request_mode.enable_thinking
        }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_api_headers(settings),
        method="POST",
    )
    started = time.monotonic()
    request_id = new_trace_id()
    first_token_ms: int | None = None
    outcome = "failed"
    diagnostic_event(
        settings,
        "chat",
        "completion_started",
        request_id=request_id,
        max_tokens=selected_max_tokens,
        temperature=selected_temperature,
        service=endpoint.name,
        request_mode=request_mode.name if request_mode is not None else "default",
        streaming=streaming,
        tool_count=len(tools or []),
        **_message_metrics(message_list),
    )
    diagnostic_milestone(
        settings,
        "llm_request_start",
        request_id=request_id,
        streaming=streaming,
    )

    def record_first_token() -> None:
        nonlocal first_token_ms
        if first_token_ms is not None:
            return
        first_token_ms = round((time.monotonic() - started) * 1000)
        diagnostic_milestone(
            settings,
            "llm_first_token",
            request_id=request_id,
            first_token_ms=first_token_ms,
        )

    try:
        response = urllib.request.urlopen(request, timeout=600)
        try:
            if not streaming:
                value = json.loads(response.read().decode("utf-8", errors="replace"))
            else:
                value = _read_complete_stream(
                    response,
                    checkpoint or (lambda: None),
                    on_content_delta=on_content_delta,
                    on_first_token=record_first_token,
                    diagnostics_source=settings,
                    request_id=request_id,
                )
        finally:
            _close_response_if_owned(response)
        if not isinstance(value, dict) or not value.get("choices"):
            raise ChatError("Модель вернула пустой ответ.")
        first_choice = value.get("choices", [{}])[0]
        message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
        if not isinstance(message, dict):
            message = {}
        tool_calls = message.get("tool_calls", [])
        elapsed_ms = round((time.monotonic() - started) * 1000)
        usage = value.get("usage", {})
        usage = usage if isinstance(usage, dict) else {}
        token_metrics = _usage_token_metrics(usage)
        prompt_tokens = token_metrics["prompt_tokens"]
        completion_tokens = token_metrics["completion_tokens"]
        diagnostic_event(
            settings,
            "chat",
            "completion_completed",
            request_id=request_id,
            duration_ms=elapsed_ms,
            first_token_ms=first_token_ms,
            output_chars=len(str(message.get("content", ""))),
            reasoning_chars=len(
                str(message.get("reasoning_content", message.get("reasoning", "")))
            ),
            tool_call_count=len(tool_calls) if isinstance(tool_calls, list) else 0,
            finish_reason=(
                first_choice.get("finish_reason", "")
                if isinstance(first_choice, dict)
                else ""
            ),
            **token_metrics,
            effective_completion_tokens_per_second=(
                round(completion_tokens * 1000 / elapsed_ms, 3)
                if completion_tokens is not None and elapsed_ms > 0
                else None
            ),
            usage_available=bool(usage),
            usage=usage,
        )
        outcome = "completed"
        return value
    except urllib.error.HTTPError as exc:
        diagnostic_event(
            settings,
            "chat",
            "completion_http_error",
            level="error",
            http_status=exc.code,
            duration_ms=round((time.monotonic() - started) * 1000),
            request_id=request_id,
        )
        detail = exc.read().decode("utf-8", errors="replace")
        raise ChatError(f"Сервер модели вернул ошибку {exc.code}: {detail}") from exc
    except json.JSONDecodeError as exc:
        diagnostic_event(
            settings,
            "chat",
            "completion_invalid_json",
            level="error",
            duration_ms=round((time.monotonic() - started) * 1000),
            request_id=request_id,
        )
        raise ChatError("Сервер модели вернул некорректный JSON.") from exc
    except (OSError, urllib.error.URLError) as exc:
        diagnostic_event(
            settings,
            "chat",
            "completion_connection_error",
            level="error",
            error_type=type(exc).__name__,
            duration_ms=round((time.monotonic() - started) * 1000),
            request_id=request_id,
        )
        raise ChatError(f"Не удалось связаться с локальной моделью: {exc}") from exc
    except Exception as exc:
        if type(exc).__name__ in {"TaskCancelled", "LiveInterrupted"}:
            outcome = "cancelled"
            diagnostic_milestone(
                settings,
                "llm_actually_cancelled",
                request_id=request_id,
                error_type=type(exc).__name__,
            )
        raise
    finally:
        diagnostic_milestone(
            settings,
            "llm_generation_end",
            request_id=request_id,
            outcome=outcome,
        )


def _read_complete_stream(
    response,
    checkpoint: Callable[[], None],
    *,
    on_content_delta: Callable[[str], None] | None = None,
    on_first_token: Callable[[], None] | None = None,
    diagnostics_source: object = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Aggregate an OpenAI-compatible stream while remaining cancellable."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    first_token_reported = False

    def report_first_token() -> None:
        nonlocal first_token_reported
        if first_token_reported or on_first_token is None:
            return
        first_token_reported = True
        on_first_token()

    for raw_line in _cancellable_response_lines(
        response,
        checkpoint,
        diagnostics_source=diagnostics_source,
        request_id=request_id,
    ):
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        choices = event.get("choices", [])
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        finish_reason = choice.get("finish_reason") or finish_reason
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            continue
        if delta.get("content") is not None:
            content_delta = str(delta["content"])
            if content_delta:
                report_first_token()
            content_parts.append(content_delta)
            if on_content_delta is not None:
                on_content_delta(content_delta)
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning is not None:
            if str(reasoning):
                report_first_token()
            reasoning_parts.append(str(reasoning))
        for raw_call in delta.get("tool_calls", []) or []:
            if not isinstance(raw_call, dict):
                continue
            report_first_token()
            index = int(raw_call.get("index", len(calls)))
            call = calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if raw_call.get("id"):
                call["id"] = str(raw_call["id"])
            if raw_call.get("type"):
                call["type"] = str(raw_call["type"])
            function = raw_call.get("function", {})
            if isinstance(function, dict):
                if function.get("name"):
                    call["function"]["name"] += str(function["name"])
                if function.get("arguments"):
                    call["function"]["arguments"] += str(function["arguments"])
    checkpoint()
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    result: dict[str, Any] = {
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ]
    }
    if usage is not None:
        result["usage"] = usage
    return result
