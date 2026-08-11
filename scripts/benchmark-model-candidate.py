from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from butler.config import Settings, load_settings  # noqa: E402
from butler.model_evaluation import base_cases, build_long_context_case, run_case  # noqa: E402
from butler.model_manager import ModelManager, RuntimeState  # noqa: E402


def gpu_snapshot() -> dict[str, object]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.used,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        output = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {}
    parts = [part.strip() for part in output.split(",")]
    if len(parts) < 4:
        return {"raw": output}
    return {
        "name": parts[0],
        "memory_used_mb": int(parts[1]),
        "memory_total_mb": int(parts[2]),
        "utilization_percent": int(parts[3]),
    }


def without_mtp(settings: Settings, role: str) -> Settings:
    raw = deepcopy(settings.raw)
    args = list(raw["models"][role].get("extra_args", []))
    for flag in ("--spec-type", "--spec-draft-n-max"):
        while flag in args:
            position = args.index(flag)
            del args[position : position + 2]
    raw["models"][role]["extra_args"] = args
    return replace(settings, raw=raw)


def verify_model_file(settings: Settings, role: str, *, verify_hash: bool) -> dict[str, object]:
    profile = settings.model(role)
    metadata = settings.raw["models"][role]
    expected_size = int(metadata.get("expected_size_bytes", 0) or 0)
    expected_hash = str(metadata.get("sha256", "")).casefold()
    if not profile.model_path.is_file():
        raise RuntimeError(f"Не найден файл модели: {profile.model_path}")
    actual_size = profile.model_path.stat().st_size
    if expected_size and actual_size != expected_size:
        raise RuntimeError(
            f"Неверный размер модели: {actual_size}, ожидалось {expected_size}"
        )
    result: dict[str, object] = {
        "path": str(profile.model_path),
        "size_bytes": actual_size,
        "size_matches": not expected_size or actual_size == expected_size,
    }
    if verify_hash:
        digest = hashlib.sha256()
        with profile.model_path.open("rb") as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        actual_hash = digest.hexdigest()
        result["sha256"] = actual_hash
        result["sha256_matches"] = not expected_hash or actual_hash == expected_hash
        if expected_hash and actual_hash != expected_hash:
            raise RuntimeError("SHA-256 модели не совпадает с опубликованным хешем.")
    return result


def restore_model(settings: Settings, original: RuntimeState | None, tested_role: str) -> None:
    manager = ModelManager(settings)
    if original is None:
        manager.stop()
        return
    if original.role == tested_role and manager.is_current(tested_role):
        return
    if original.role not in settings.model_roles():
        manager.stop()
        raise RuntimeError(
            f"Нельзя автоматически восстановить неизвестный профиль {original.role!r}."
        )
    manager.start(original.role)


def main() -> int:
    parser = argparse.ArgumentParser(description="Безопасная локальная проверка модели-кандидата")
    parser.add_argument("--role", default="developer_qwopus")
    parser.add_argument("--mtp", choices=("on", "off"), default="on")
    parser.add_argument("--long-context", action="store_true")
    parser.add_argument(
        "--context-tokens",
        type=int,
        default=0,
        help="Добавить тест метки с заданным размером входа; безопасный диапазон 1000–60000.",
    )
    parser.add_argument("--verify-hash", action="store_true")
    parser.add_argument(
        "--plan-tokens",
        type=int,
        default=480,
        help="Лимит ответа для длинного структурированного плана, от 480 до 4096.",
    )
    args = parser.parse_args()
    if not 480 <= args.plan_tokens <= 4096:
        parser.error("--plan-tokens должен быть от 480 до 4096.")

    base_settings = load_settings(ROOT)
    test_settings = (
        base_settings if args.mtp == "on" else without_mtp(base_settings, args.role)
    )
    base_manager = ModelManager(base_settings)
    original = base_manager.running_state()
    report: dict[str, object] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "role": args.role,
        "mtp": args.mtp,
        "original_state": asdict(original) if original else None,
        "gpu_before": gpu_snapshot(),
        "tests": [],
    }
    output_dir = base_settings.runtime_dir / "benchmarks"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = output_dir / f"{args.role}-{args.mtp}-{stamp}.json"

    try:
        report["model_file"] = verify_model_file(
            test_settings, args.role, verify_hash=args.verify_hash
        )
        print(f"Запускаю профиль {args.role}, MTP: {args.mtp}.", flush=True)
        state = ModelManager(test_settings).start(args.role)
        report["test_state"] = asdict(state)
        report["gpu_loaded"] = gpu_snapshot()
        cases = base_cases()
        if args.plan_tokens != 480:
            cases = [
                replace(case, max_tokens=args.plan_tokens)
                if case.name == "structured_russian_plan"
                else case
                for case in cases
            ]
        report["structured_plan_max_tokens"] = args.plan_tokens
        context_tokens = 48_000 if args.long_context else int(args.context_tokens or 0)
        if context_tokens:
            if not 1_000 <= context_tokens <= 60_000:
                raise ValueError("--context-tokens должен быть от 1000 до 60000.")
            long_case, token_count = build_long_context_case(
                test_settings, target_tokens=context_tokens
            )
            report["long_context_target_tokens"] = context_tokens
            report["long_context_input_tokens"] = token_count
            cases.append(long_case)
        for index, case in enumerate(cases, 1):
            print(f"[{index}/{len(cases)}] {case.description}", flush=True)
            try:
                result = run_case(test_settings, case)
            except Exception as exc:  # Keep the remaining independent checks running.
                result = {
                    "name": case.name,
                    "description": case.description,
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            report["tests"].append(result)
            status = "ПРОЙДЕН" if result.get("passed") else "НЕ ПРОЙДЕН"
            print(f"    {status}", flush=True)
        tests = report["tests"]
        passed = sum(1 for item in tests if item.get("passed"))
        report["summary"] = {"passed": passed, "total": len(tests)}
        report["gpu_after_tests"] = gpu_snapshot()
    except Exception as exc:
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            restore_model(base_settings, original, args.role)
            report["restored_state"] = (
                asdict(ModelManager(base_settings).running_state())
                if ModelManager(base_settings).running_state()
                else None
            )
        except Exception as exc:
            report["restore_error"] = f"{type(exc).__name__}: {exc}"
        report["finished_at"] = datetime.now().astimezone().isoformat()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Отчёт: {output}", flush=True)

    if report.get("fatal_error") or report.get("restore_error"):
        return 2
    summary = report.get("summary", {})
    return 0 if summary.get("passed") == summary.get("total") else 1


if __name__ == "__main__":
    raise SystemExit(main())
