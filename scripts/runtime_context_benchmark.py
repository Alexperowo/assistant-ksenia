from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from butler.atomic_io import atomic_write_text  # noqa: E402
from butler.config import Settings, load_settings  # noqa: E402
from butler.local_auth import local_api_key  # noqa: E402
from butler.model_manager import ModelManager, RuntimeState  # noqa: E402
from butler.tools import tool_schemas  # noqa: E402


DEFAULT_CONTEXTS = (16_384, 32_768, 49_152, 98_304)


def parse_contexts(raw: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Контексты должны быть целыми числами.") from exc
    if not values:
        raise argparse.ArgumentTypeError("Нужен хотя бы один размер контекста.")
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("Размеры контекста не должны повторяться.")
    if any(value < 512 or value > 1_048_576 for value in values):
        raise argparse.ArgumentTypeError("Контекст должен быть от 512 до 1048576.")
    return values


def settings_with_context(settings: Settings, role: str, context_size: int) -> Settings:
    raw = deepcopy(settings.raw)
    try:
        profile = raw["models"][role]
        profile["context_size"] = int(context_size)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Неизвестный профиль модели: {role}") from exc
    return replace(settings, raw=raw)


def canonical_tool_fingerprint(settings: Settings) -> dict[str, object]:
    schemas = tool_schemas(settings)
    payload = json.dumps(
        schemas,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "count": len(schemas),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def gpu_snapshot() -> list[dict[str, object]]:
    command = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    result: list[dict[str, object]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            continue
        try:
            result.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "memory_used_mb": int(parts[2]),
                    "memory_total_mb": int(parts[3]),
                    "utilization_percent": int(parts[4]),
                }
            )
        except ValueError:
            continue
    return result


def process_memory(pid: int) -> dict[str, int]:
    if sys.platform != "win32":
        return {}

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    psapi.GetProcessMemoryInfo.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCountersEx),
        ctypes.c_ulong,
    )
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not handle:
        return {}
    try:
        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return {}
        return {
            "working_set_bytes": int(counters.WorkingSetSize),
            "private_bytes": int(counters.PrivateUsage),
            "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
        }
    finally:
        kernel32.CloseHandle(handle)


def _usage_metrics(value: object) -> dict[str, int | None]:
    usage = value if isinstance(value, dict) else {}
    details = usage.get("prompt_tokens_details", {})
    details = details if isinstance(details, dict) else {}

    def optional_int(raw: object) -> int | None:
        try:
            return max(0, int(raw)) if raw is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "prompt_tokens": optional_int(usage.get("prompt_tokens")),
        "completion_tokens": optional_int(usage.get("completion_tokens")),
        "cached_tokens": optional_int(details.get("cached_tokens")),
    }


def request_chat(
    settings: Settings,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[str, dict[str, object]]:
    payload = {
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
        "cache_prompt": True,
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        f"http://{settings.host}:{settings.port}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {local_api_key(settings)}",
        },
        method="POST",
    )
    started = time.perf_counter()
    first_token_ms: float | None = None
    content: list[str] = []
    usage: dict[str, object] = {}
    with urllib.request.urlopen(request, timeout=600) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                event = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            choices = event.get("choices", [])
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
            if not isinstance(delta, dict):
                continue
            text = delta.get("content")
            reasoning = delta.get("reasoning_content", delta.get("reasoning"))
            if (text or reasoning) and first_token_ms is None:
                first_token_ms = (time.perf_counter() - started) * 1000
            if isinstance(text, str):
                content.append(text)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return "".join(content), {
        "duration_ms": round(elapsed_ms, 3),
        "ttft_ms": round(first_token_ms, 3) if first_token_ms is not None else None,
        "output_chars": sum(len(item) for item in content),
        **_usage_metrics(usage),
    }


def run_prompt_pair(settings: Settings, *, max_tokens: int) -> dict[str, object]:
    schemas = tool_schemas(settings)
    system = (
        "Ты Ксения, локальный ассистент Александра. Отвечай по-русски, кратко и "
        "точно. Не вызывай инструмент, если вопрос решается без него."
    )
    first_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "Ответь одним словом: сколько будет два плюс два?"},
    ]
    first_text, first = request_chat(
        settings, first_messages, tools=schemas, max_tokens=max_tokens
    )
    second_messages = [
        *first_messages,
        {"role": "assistant", "content": first_text},
        {"role": "user", "content": "Ответь одним словом: сколько будет три плюс три?"},
    ]
    _second_text, second = request_chat(
        settings, second_messages, tools=schemas, max_tokens=max_tokens
    )
    return {"first_turn": first, "next_turn": second}


def restore_model(settings: Settings, original: RuntimeState | None) -> None:
    manager = ModelManager(settings)
    manager.stop()
    if original is not None:
        manager.start(original.role)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Измерить load/TTFT/KV reuse при разных размерах runtime context."
    )
    parser.add_argument("--profile", default="generalist")
    parser.add_argument(
        "--contexts",
        type=parse_contexts,
        default=DEFAULT_CONTEXTS,
        help="Размеры через запятую; по умолчанию 16384,32768,49152,98304.",
    )
    parser.add_argument("--max-tokens", type=int, default=32)
    args = parser.parse_args()
    if not 8 <= args.max_tokens <= 256:
        parser.error("--max-tokens должен быть от 8 до 256.")

    base_settings = load_settings(ROOT)
    if args.profile not in base_settings.model_roles():
        parser.error(f"Неизвестный профиль: {args.profile}")
    profile = base_settings.model(args.profile)
    if not profile.enabled:
        parser.error(f"Профиль {args.profile} выключен; benchmark не включает его сам.")
    if not profile.model_path.is_file():
        parser.error(f"Не найден артефакт профиля {args.profile}.")

    base_manager = ModelManager(base_settings)
    original = base_manager.running_state()
    output_dir = base_settings.runtime_dir / "benchmarks"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = output_dir / f"{args.profile}-runtime-context-{stamp}.json"
    report: dict[str, object] = {
        "schema_version": 1,
        "started_at": datetime.now().astimezone().isoformat(),
        "profile": args.profile,
        "backend": profile.backend.name if profile.backend else "default",
        "contexts": list(args.contexts),
        "max_tokens": args.max_tokens,
        "tool_schema": canonical_tool_fingerprint(base_settings),
        "original_state": asdict(original) if original else None,
        "results": [],
    }
    exit_code = 0
    try:
        for context_size in args.contexts:
            print(f"Контекст {context_size}: запускаю {args.profile}.", flush=True)
            test_settings = settings_with_context(
                base_settings, args.profile, context_size
            )
            manager = ModelManager(test_settings)
            item: dict[str, object] = {
                "context_size": context_size,
                "gpu_before": gpu_snapshot(),
            }
            try:
                manager.stop()
                started = time.perf_counter()
                state = manager.start(args.profile)
                item["load_ms"] = round((time.perf_counter() - started) * 1000, 3)
                item["actual_context"] = state.actual_context
                item["gpu_loaded"] = gpu_snapshot()
                item["process_memory_loaded"] = process_memory(state.pid)
                item["prompt_pair"] = run_prompt_pair(
                    test_settings, max_tokens=args.max_tokens
                )
                item["gpu_after"] = gpu_snapshot()
                item["process_memory_after"] = process_memory(state.pid)
                item["ok"] = True
            except Exception as exc:
                item["ok"] = False
                item["error"] = f"{type(exc).__name__}: {exc}"
                exit_code = 1
            finally:
                manager.stop()
            report["results"].append(item)
            atomic_write_text(
                output,
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            )
    finally:
        try:
            restore_model(base_settings, original)
            restored = ModelManager(base_settings).running_state()
            report["restored_state"] = asdict(restored) if restored else None
        except Exception as exc:
            report["restore_error"] = f"{type(exc).__name__}: {exc}"
            exit_code = 2
        report["finished_at"] = datetime.now().astimezone().isoformat()
        atomic_write_text(
            output,
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        )
        print(f"Отчёт: {output}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
