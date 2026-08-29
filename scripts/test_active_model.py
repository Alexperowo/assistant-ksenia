from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from butler.chat import complete_chat  # noqa: E402
from butler.config import load_settings  # noqa: E402
from butler.diagnostics import new_trace_id, trace_scope  # noqa: E402
from butler.model_manager import ModelManager  # noqa: E402


def main() -> int:
    settings = load_settings(ROOT)
    manager = ModelManager(settings)
    state = manager.running_state()
    report: dict[str, object] = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "active": state is not None,
    }
    if state is None:
        report.update({"skipped": True, "reason": "локальная модель не запущена"})
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if not manager.api_ready(timeout=2):
        report.update({"passed": False, "reason": "API активной модели не отвечает"})
        print(json.dumps(report, ensure_ascii=False))
        return 1

    started = time.monotonic()
    trace_id = new_trace_id()
    with trace_scope(
        run_id="active-model-smoke",
        trace_id=trace_id,
        turn_id=trace_id,
    ):
        response = complete_chat(
            settings,
            [
                {
                    "role": "system",
                    "content": "Ты локальная русскоязычная помощница Ксения. Выполни формат буквально.",
                },
                {"role": "user", "content": "Ответь ровно одним словом: Ксения"},
            ],
            tools=None,
            temperature=0.0,
            max_tokens=24,
            checkpoint=lambda: None,
        )
    message = response["choices"][0].get("message", {})
    answer = str(message.get("content") or "").strip()
    words = re.findall(r"[а-яё]+", answer.casefold())
    passed = "ксения" in words and "tool_call" not in answer.casefold()
    metadata = manager.model_metadata(timeout=2)
    report.update(
        {
            "skipped": False,
            "passed": passed,
            "role": state.role,
            "pid": state.pid,
            "trace_id": trace_id,
            "answer": answer,
            "duration_seconds": round(time.monotonic() - started, 3),
            "actual_context": int(metadata.get("n_ctx", state.actual_context) or 0),
        }
    )
    target = settings.runtime_dir / "audit" / "active-model-latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
