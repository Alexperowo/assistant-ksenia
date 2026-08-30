from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from butler.atomic_io import atomic_write_text  # noqa: E402
from butler.config import Settings, load_settings  # noqa: E402
from butler.model_assets import model_asset_from_config, verify_model_path  # noqa: E402
from butler.model_evaluation import (  # noqa: E402
    base_cases,
    build_long_context_case,
    run_case,
    parse_speculative_metrics,
    with_acceleration_mode,
)
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


def with_profile_enabled(settings: Settings, role: str) -> Settings:
    raw = deepcopy(settings.raw)
    try:
        raw["models"][role]["enabled"] = True
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Неизвестный профиль модели: {role}") from exc
    return replace(settings, raw=raw)


def verify_model_file(settings: Settings, role: str, *, verify_hash: bool) -> dict[str, object]:
    profile = settings.model(role)
    metadata = settings.raw["models"][role]
    if isinstance(metadata.get("artifacts"), dict):
        asset = model_asset_from_config(settings.raw["models"], role)
        # ModelManager performs the mandatory full hash check (or accepts its
        # identity-bound integrity cache) immediately before Popen. This optional
        # independent pass is useful for supply-chain audits, but repeating it for
        # every point of a sweep would read a 40–70 GiB model twice per launch.
        return verify_model_path(
            asset,
            profile.model_path,
            verify_hash=verify_hash,
        )

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
    parser.add_argument("--profile", "--role", dest="profile", default="candidate")
    parser.add_argument(
        "--acceleration",
        "--mtp",
        dest="acceleration",
        choices=("on", "off"),
        default="on",
        help="Включить или выключить ускорение, объявленное выбранным профилем.",
    )
    parser.add_argument(
        "--spec-tokens",
        type=int,
        default=0,
        help="Переопределить acceleration.max_tokens только в памяти; 1–32.",
    )
    parser.add_argument(
        "--acceleration-type",
        choices=("draft-mtp", "draft-dflash"),
        default=None,
        help=(
            "Явно выбрать экспериментальное ускорение только для этого прогона; "
            "постоянная конфигурация не меняется."
        ),
    )
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
        default=768,
        help="Лимит ответа для длинного структурированного плана, по умолчанию 768; допустимо 480–4096.",
    )
    args = parser.parse_args()
    if not 480 <= args.plan_tokens <= 4096:
        parser.error("--plan-tokens должен быть от 480 до 4096.")
    if args.spec_tokens and not 1 <= args.spec_tokens <= 32:
        parser.error("--spec-tokens должен быть от 1 до 32.")
    if args.acceleration == "off" and args.spec_tokens:
        parser.error("--spec-tokens нельзя задавать при --acceleration off.")
    if args.acceleration == "off" and args.acceleration_type:
        parser.error("--acceleration-type нельзя задавать при --acceleration off.")

    base_settings = load_settings(ROOT)
    candidate_settings = with_profile_enabled(base_settings, args.profile)
    test_settings = with_acceleration_mode(
        candidate_settings,
        args.profile,
        enabled=args.acceleration == "on",
        max_tokens=args.spec_tokens or None,
        acceleration_type=args.acceleration_type,
    )
    base_manager = ModelManager(base_settings)
    original = base_manager.running_state()
    report: dict[str, object] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "profile": args.profile,
        "acceleration": args.acceleration,
        "acceleration_type": test_settings.model(args.profile).acceleration_type,
        "spec_tokens": test_settings.model(args.profile).acceleration_max_tokens,
        "original_state": asdict(original) if original else None,
        "gpu_before": gpu_snapshot(),
        "tests": [],
    }
    output_dir = base_settings.runtime_dir / "benchmarks"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = output_dir / f"{args.profile}-{args.acceleration}-{stamp}.json"

    try:
        report["model_file"] = verify_model_file(
            test_settings, args.profile, verify_hash=args.verify_hash
        )
        print(
            f"Запускаю профиль {args.profile}, ускорение: {args.acceleration}, "
            f"тип: {report['acceleration_type']}, tokens: {report['spec_tokens']}.",
            flush=True,
        )
        load_started = time.perf_counter()
        state = ModelManager(test_settings).start(args.profile)
        report["load_ms"] = round((time.perf_counter() - load_started) * 1000, 3)
        report["test_state"] = asdict(state)
        report["gpu_loaded"] = gpu_snapshot()
        cases = base_cases()
        if args.plan_tokens != 768:
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
        report["speculative_metrics"] = parse_speculative_metrics(
            Path(state.log_path) if state.log_path else None
        )
    except Exception as exc:
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            restore_model(base_settings, original, args.profile)
            report["restored_state"] = (
                asdict(ModelManager(base_settings).running_state())
                if ModelManager(base_settings).running_state()
                else None
            )
        except Exception as exc:
            report["restore_error"] = f"{type(exc).__name__}: {exc}"
        report["finished_at"] = datetime.now().astimezone().isoformat()
        atomic_write_text(
            output,
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        )
        print(f"Отчёт: {output}", flush=True)

    if report.get("fatal_error") or report.get("restore_error"):
        return 2
    summary = report.get("summary", {})
    return 0 if summary.get("passed") == summary.get("total") else 1


if __name__ == "__main__":
    raise SystemExit(main())
