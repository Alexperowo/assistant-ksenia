from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from butler.chat import _reader_counts, complete_chat  # noqa: E402
from butler.config import load_settings  # noqa: E402
from butler.diagnostics import new_trace_id, trace_scope  # noqa: E402
from butler.model_manager import ModelManager  # noqa: E402
from butler.tasking import TaskCancelled  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверить фактическую отмену активного streaming LLM-запроса."
    )
    parser.add_argument("--cancel-after-ms", type=int, default=500)
    args = parser.parse_args()

    settings = load_settings(ROOT)
    state = ModelManager(settings).running_state()
    if state is None:
        print(
            json.dumps(
                {
                    "active": False,
                    "skipped": True,
                    "reason": "локальная модель не запущена",
                },
                ensure_ascii=False,
            )
        )
        return 0

    cancel_after_ms = max(50, int(args.cancel_after_ms))
    trace_id = new_trace_id()
    started = time.monotonic()

    def checkpoint() -> None:
        elapsed_ms = (time.monotonic() - started) * 1000
        if elapsed_ms >= cancel_after_ms:
            raise TaskCancelled("Контрольная отмена streaming-запроса.")

    cancelled = False
    cancellation_exception_ms: int | None = None
    with trace_scope(
        run_id="active-model-cancellation",
        trace_id=trace_id,
        turn_id=trace_id,
    ):
        try:
            complete_chat(
                settings,
                [
                    {
                        "role": "system",
                        "content": "Отвечай по-русски обычным текстом.",
                    },
                    {
                        "role": "user",
                        "content": (
                            "Подробно объясни устройство локального голосового ассистента, "
                            "продолжая ответ до остановки запроса."
                        ),
                    },
                ],
                checkpoint=checkpoint,
                max_tokens=4096,
            )
        except TaskCancelled:
            cancelled = True
            cancellation_exception_ms = round((time.monotonic() - started) * 1000)

    deadline = time.monotonic() + 1.0
    counts = _reader_counts()
    while counts["active_reader_threads"] and time.monotonic() < deadline:
        time.sleep(0.01)
        counts = _reader_counts()

    report = {
        "active": True,
        "skipped": False,
        "passed": bool(
            cancelled
            and counts["active_reader_threads"] == 0
            and counts["stuck_reader_threads"] == 0
        ),
        "role": state.role,
        "pid": state.pid,
        "trace_id": trace_id,
        "cancel_after_ms": cancel_after_ms,
        "cancellation_exception_ms": cancellation_exception_ms,
        "cleanup_elapsed_ms": round((time.monotonic() - started) * 1000),
        **counts,
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
