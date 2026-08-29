from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from butler.performance import (
    LoadedEvents,
    build_report,
    diagnostic_log_paths,
    format_report,
    load_events,
    metric_stats,
    percentile,
)


def _event(
    name: str,
    monotonic_ms: int,
    *,
    trace_id: str = "trace-1",
    component: str = "performance",
    **fields: object,
) -> dict[str, object]:
    return {
        "time": "2026-08-29T10:00:00+00:00",
        "monotonic_ns": monotonic_ms * 1_000_000,
        "session_id": "session-1",
        "trace_id": trace_id,
        "task_id": "task-1",
        "component": component,
        "event": name,
        **fields,
    }


class PerformanceTests(unittest.TestCase):
    def test_percentiles_and_summary_are_deterministic(self):
        values = [40, 10, 20, 30]
        self.assertEqual(percentile(values, 0.5), 25)
        self.assertEqual(percentile(values, 0.95), 38.5)
        self.assertEqual(
            metric_stats(values).as_dict(),
            {
                "count": 4,
                "min": 10,
                "average": 25,
                "p50": 25,
                "p95": 38.5,
                "max": 40,
            },
        )

    def test_loader_keeps_valid_rotated_lines_and_counts_damage(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            logs = runtime_dir / "logs"
            logs.mkdir()
            (logs / "diagnostics.jsonl.2").write_text(
                json.dumps(_event("older", 1)) + "\n{broken\n",
                encoding="utf-8",
            )
            (logs / "diagnostics.jsonl").write_text(
                json.dumps(_event("newer", 2)) + "\n",
                encoding="utf-8",
            )
            loaded = load_events(diagnostic_log_paths(runtime_dir))
            self.assertEqual([item["event"] for item in loaded.events], ["older", "newer"])
            self.assertEqual(loaded.invalid_line_count, 1)

    def test_report_builds_waterfall_metrics_and_marks_incomplete_traces(self):
        events = [
            _event("voice_start", 100),
            _event("voice_end", 1100),
            _event("turn_detected", 1180),
            _event("stt_final", 1500),
            _event("llm_request_start", 1600),
            _event("llm_first_token", 1900),
            _event("llm_generation_end", 2400),
            _event("tts_first_chunk_ready", 2050),
            _event("audio_first_played", 2100),
            _event("audio_finished", 3000),
            _event(
                "completion_completed",
                2400,
                component="chat",
                duration_ms=800,
            ),
            _event("voice_start", 4000, trace_id="trace-2"),
        ]
        report = build_report(LoadedEvents(tuple(events), 2, ()))
        self.assertEqual(len(report.traces), 2)
        first = next(item for item in report.traces if item.trace_id == "trace-1")
        second = next(item for item in report.traces if item.trace_id == "trace-2")
        self.assertEqual(first.missing_milestones, ())
        self.assertIn("stt_final", second.missing_milestones)
        self.assertEqual(report.metrics["voice_duration_ms"].p50, 1000)
        self.assertEqual(report.metrics["llm_ttft_ms"].p50, 300)
        self.assertEqual(report.metrics["voice_end_to_audio_ms"].p50, 1000)
        self.assertEqual(
            report.metrics["chat.completion_completed.duration_ms"].p50,
            800,
        )
        rendered = format_report(report)
        self.assertIn("полных: 1", rendered)
        self.assertIn("повреждённых строк: 2", rendered)

    def test_last_milestone_uses_latest_audio_completion(self):
        events = (
            _event("audio_first_played", 100),
            _event("audio_finished", 200),
            _event("audio_finished", 500),
        )
        report = build_report(
            LoadedEvents(events, 0, ()), required_milestones=("audio_finished",)
        )
        self.assertEqual(report.traces[0].milestones_ms["audio_finished"], 400)

    def test_activation_and_progress_audio_before_first_token_are_not_ttfa(self):
        events = (
            _event("tts_first_chunk_ready", 10),
            _event("audio_first_played", 20),
            _event("audio_finished", 80),
            _event("llm_request_start", 100),
            _event("llm_first_token", 300),
            _event("tts_first_chunk_ready", 350),
            _event("audio_first_played", 370),
            _event("audio_finished", 800),
        )
        report = build_report(
            LoadedEvents(events, 0, ()),
            required_milestones=("audio_first_played",),
        )
        milestones = report.traces[0].milestones_ms
        self.assertEqual(milestones["tts_first_chunk_ready"], 340)
        self.assertEqual(milestones["audio_first_played"], 360)
        self.assertEqual(milestones["audio_finished"], 790)


if __name__ == "__main__":
    unittest.main()
