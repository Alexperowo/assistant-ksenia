from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from butler.config import load_settings  # noqa: E402
from butler.model_manager import ModelManager  # noqa: E402
from butler.orchestrator import RoutedAgentSession  # noqa: E402


DEFAULT_QUERY = (
    "Найди на официальном сайте Python страницу релиза Python 3.12.10, "
    "назови дату выпуска и дай прямую ссылку. Ничего не скачивай."
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Изолированный живой замер исследовательского маршрута"
    )
    parser.add_argument("query", nargs="?", default=DEFAULT_QUERY)
    args = parser.parse_args()
    base = load_settings(ROOT)
    original = ModelManager(base).running_state()
    output_dir = base.runtime_dir / "benchmarks"
    test_root = base.runtime_dir / "research-tests"
    output_dir.mkdir(parents=True, exist_ok=True)
    test_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "query": args.query,
        "original_state": asdict(original) if original else None,
        "statuses": [],
    }
    started = time.monotonic()

    with tempfile.TemporaryDirectory(
        dir=test_root, ignore_cleanup_errors=True
    ) as directory:
        settings = replace(base, runtime_dir=Path(directory))
        manager = ModelManager(settings)

        def status(value: str) -> None:
            report["statuses"].append(value)
            print(f"[СТАТУС] {value}", flush=True)

        try:
            reply = RoutedAgentSession(settings).ask(args.query, on_status=status)
            events = [
                {
                    "name": event.name,
                    "status": event.result.status,
                    "ok": event.result.ok,
                }
                for event in reply.tool_events
            ]
            browser_used = any(
                str(event["name"]).startswith("browser_") for event in events
            )
            expected_url = "https://www.python.org/downloads/release/python-31210/"
            expected_date = bool(
                re.search(
                    r"(?:8\s+апрел\w*\s+2025|April\s+8,?\s+2025|2025-04-08)",
                    reply.text,
                    flags=re.IGNORECASE,
                )
            )
            default_case = args.query == DEFAULT_QUERY
            report.update(
                {
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "answer": reply.text,
                    "tool_events": events,
                    "research_model": RoutedAgentSession(settings)._research_model(),
                    "expected_url_found": expected_url in reply.text if default_case else None,
                    "expected_date_found": expected_date if default_case else None,
                    "passed": bool(
                        browser_used
                        and reply.text.strip()
                        and (
                            not default_case
                            or (expected_url in reply.text and expected_date)
                        )
                    ),
                }
            )
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            report["passed"] = False
        finally:
            report["model_stopped"] = manager.stop()
            report["port_released"] = manager._wait_port_closed()

    if original:
        ModelManager(base).start(original.role)
    report["restored_state"] = (
        asdict(ModelManager(base).running_state())
        if ModelManager(base).running_state()
        else None
    )
    report["finished_at"] = datetime.now().astimezone().isoformat()
    output = output_dir / "research-live-latest.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Отчёт: {output}")
    return 0 if report.get("passed") and report.get("port_released") else 1


if __name__ == "__main__":
    raise SystemExit(main())
