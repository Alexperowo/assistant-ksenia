from __future__ import annotations

import argparse
import json
import sys
import tempfile
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from butler.config import load_settings  # noqa: E402
from butler.model_manager import ModelManager  # noqa: E402
from butler.orchestrator import RoutedAgentSession  # noqa: E402


REQUEST = (
    "Прочитай файл README.txt в рабочей папке и кратко, по-русски, "
    "назови два правила работы Ксении с файлами. Ничего не изменяй."
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Изолированный сквозной тест модели-исполнителя"
    )
    parser.add_argument("--profile", default="candidate")
    args = parser.parse_args()

    base = load_settings(ROOT)
    raw = deepcopy(base.raw)
    try:
        raw["models"][args.profile]["enabled"] = True
    except (KeyError, TypeError):
        parser.error(f"неизвестный профиль: {args.profile}")
    raw["capability_roles"]["developer"]["primary_model"] = args.profile
    test_root = base.runtime_dir / "agent-candidate-tests"
    test_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "profile": args.profile,
        "request": REQUEST,
    }
    with tempfile.TemporaryDirectory(
        dir=test_root, ignore_cleanup_errors=True
    ) as directory:
        settings = replace(base, raw=raw, runtime_dir=Path(directory))
        manager = ModelManager(settings)
        try:
            reply = RoutedAgentSession(settings).ask(REQUEST)
            events = [
                {
                    "name": event.name,
                    "arguments": event.arguments,
                    "result": event.result.as_dict(),
                }
                for event in reply.tool_events
            ]
            normalized = reply.text.casefold()
            read_used = any(
                event["name"] == "read_workspace_file" for event in events
            )
            mentions_workspace = "папк" in normalized or "workspace" in normalized
            mentions_confirmation = "подтверж" in normalized
            report.update(
                {
                    "answer": reply.text,
                    "tool_events": events,
                    "running_state": (
                        asdict(manager.running_state())
                        if manager.running_state()
                        else None
                    ),
                    "checks": {
                        "read_tool_used": read_used,
                        "workspace_rule_present": mentions_workspace,
                        "confirmation_rule_present": mentions_confirmation,
                    },
                    "passed": bool(
                        read_used and mentions_workspace and mentions_confirmation
                    ),
                }
            )
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            report["passed"] = False
        finally:
            report["model_stopped"] = manager.stop()
            report["port_released"] = manager._wait_port_closed()

    report["finished_at"] = datetime.now().astimezone().isoformat()
    output = base.runtime_dir / "benchmarks" / f"agent-{args.profile}-latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Отчёт: {output}")
    return 0 if report.get("passed") and report.get("port_released") else 1


if __name__ == "__main__":
    raise SystemExit(main())
