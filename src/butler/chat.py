from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from butler.config import Settings
from butler.diagnostics import event as diagnostic_event
from butler.local_auth import local_api_key


class ChatError(RuntimeError):
    pass


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
            if match and len(match.group(1).strip()) >= self.minimum_length:
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


def count_chat_tokens(settings: Settings, messages: Iterable[dict[str, Any]]) -> int:
    """Count tokens with the active llama.cpp tokenizer, with a safe fallback."""
    message_list = normalize_system_messages(messages)
    content = json.dumps(message_list, ensure_ascii=False, separators=(",", ":"))
    started = time.monotonic()
    request = urllib.request.Request(
        f"http://{settings.host}:{settings.port}/tokenize",
        data=json.dumps(
            {"content": content, "add_special": False}, ensure_ascii=False
        ).encode("utf-8"),
        headers=_api_headers(settings),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8", errors="replace"))
        tokens = value.get("tokens", []) if isinstance(value, dict) else []
        if isinstance(tokens, list):
            diagnostic_event(
                settings,
                "chat",
                "token_count_completed",
                duration_ms=round((time.monotonic() - started) * 1000),
                token_count=len(tokens),
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
            **_message_metrics(message_list),
        )
    fallback = max(1, len(content) // 2)
    return fallback


def stream_chat(
    settings: Settings,
    messages: Iterable[dict[str, Any]],
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> Iterator[str]:
    message_list = normalize_system_messages(messages)
    selected_max_tokens = default_max_tokens(settings) if max_tokens is None else max_tokens
    url = f"http://{settings.host}:{settings.port}/v1/chat/completions"
    body = json.dumps(
        {
            "messages": message_list,
            "stream": True,
            "temperature": temperature,
            "max_tokens": selected_max_tokens,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers=_api_headers(settings),
        method="POST",
    )
    started = time.monotonic()
    first_token_ms: int | None = None
    output_chars = 0
    completed = False
    diagnostic_event(
        settings,
        "chat",
        "stream_started",
        max_tokens=selected_max_tokens,
        temperature=temperature,
        **_message_metrics(message_list),
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                    delta = event["choices"][0].get("delta", {})
                    content = delta.get("content")
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                if content:
                    if first_token_ms is None:
                        first_token_ms = round((time.monotonic() - started) * 1000)
                    output_chars += len(str(content))
                    yield str(content)
        completed = True
    except urllib.error.HTTPError as exc:
        diagnostic_event(
            settings,
            "chat",
            "stream_http_error",
            level="error",
            http_status=exc.code,
            duration_ms=round((time.monotonic() - started) * 1000),
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
        )
        raise ChatError(f"Не удалось связаться с локальной моделью: {exc}") from exc
    finally:
        diagnostic_event(
            settings,
            "chat",
            "stream_completed" if completed else "stream_closed",
            level="info" if completed else "warning",
            duration_ms=round((time.monotonic() - started) * 1000),
            first_token_ms=first_token_ms,
            output_chars=output_chars,
        )


def complete_chat(
    settings: Settings,
    messages: Iterable[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Request one complete response, optionally exposing safe tools to the model."""
    url = f"http://{settings.host}:{settings.port}/v1/chat/completions"
    message_list = normalize_system_messages(messages)
    selected_max_tokens = default_max_tokens(settings) if max_tokens is None else max_tokens
    payload: dict[str, Any] = {
        "messages": message_list,
        "stream": checkpoint is not None,
        "temperature": temperature,
        "max_tokens": selected_max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_api_headers(settings),
        method="POST",
    )
    started = time.monotonic()
    diagnostic_event(
        settings,
        "chat",
        "completion_started",
        max_tokens=selected_max_tokens,
        temperature=temperature,
        streaming=checkpoint is not None,
        tool_count=len(tools or []),
        **_message_metrics(message_list),
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if checkpoint is None:
                value = json.loads(response.read().decode("utf-8", errors="replace"))
            else:
                value = _read_complete_stream(response, checkpoint)
        if not isinstance(value, dict) or not value.get("choices"):
            raise ChatError("Модель вернула пустой ответ.")
        first_choice = value.get("choices", [{}])[0]
        message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
        if not isinstance(message, dict):
            message = {}
        tool_calls = message.get("tool_calls", [])
        diagnostic_event(
            settings,
            "chat",
            "completion_completed",
            duration_ms=round((time.monotonic() - started) * 1000),
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
            usage=value.get("usage", {}),
        )
        return value
    except urllib.error.HTTPError as exc:
        diagnostic_event(
            settings,
            "chat",
            "completion_http_error",
            level="error",
            http_status=exc.code,
            duration_ms=round((time.monotonic() - started) * 1000),
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
        )
        raise ChatError(f"Не удалось связаться с локальной моделью: {exc}") from exc


def _read_complete_stream(response, checkpoint: Callable[[], None]) -> dict[str, Any]:
    """Aggregate an OpenAI-compatible stream while remaining cancellable."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    for raw_line in response:
        checkpoint()
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
            content_parts.append(str(delta["content"]))
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning is not None:
            reasoning_parts.append(str(reasoning))
        for raw_call in delta.get("tool_calls", []) or []:
            if not isinstance(raw_call, dict):
                continue
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
