from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from butler.performance import (  # noqa: E402
    DEFAULT_REQUIRED_MILESTONES,
    build_report,
    diagnostic_log_paths,
    format_report,
    load_events,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сводка реальных задержек по безопасному JSONL Ксении"
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=ROOT / "runtime",
        help="runtime-каталог; по умолчанию runtime проекта",
    )
    parser.add_argument(
        "--input",
        action="append",
        type=Path,
        default=[],
        help="явный JSONL-файл; параметр можно повторить",
    )
    parser.add_argument(
        "--required-milestone",
        action="append",
        default=None,
        help="обязательный milestone; параметр можно повторить",
    )
    parser.add_argument("--trace-id", help="оставить только одну трассу")
    parser.add_argument("--json", action="store_true", help="вывести машинный JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = args.input or diagnostic_log_paths(args.runtime_dir.resolve())
    loaded = load_events(path.resolve() for path in paths)
    if args.trace_id:
        loaded = type(loaded)(
            tuple(
                event
                for event in loaded.events
                if str(event.get("trace_id", "")) == args.trace_id
            ),
            loaded.invalid_line_count,
            loaded.paths,
        )
    required = (
        tuple(args.required_milestone)
        if args.required_milestone is not None
        else DEFAULT_REQUIRED_MILESTONES
    )
    report = build_report(loaded, required_milestones=required)
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
