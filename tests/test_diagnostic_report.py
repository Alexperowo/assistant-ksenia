import tempfile
import unittest
import json
import os
from pathlib import Path
from types import SimpleNamespace

from butler.diagnostic_report import format_summary, summarize
from butler.diagnostics import event


class DiagnosticReportTests(unittest.TestCase):
    def test_summary_counts_events_and_problems(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            settings = SimpleNamespace(
                runtime_dir=runtime_dir,
                raw={
                    "diagnostics": {
                        "enabled": True,
                        "allow_during_tests": True,
                    }
                },
            )
            event(settings, "voice", "ready")
            event(settings, "voice", "late", level="warning", detail="slow")
            state_path = runtime_dir / "voice" / "agent.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            summary = summarize(runtime_dir)
            self.assertEqual(summary.event_count, 2)
            self.assertEqual(summary.invalid_line_count, 0)
            self.assertEqual(summary.components["voice"], 2)
            self.assertEqual(len(summary.recent_problems), 1)
            self.assertEqual(summary.active_voice_pid, os.getpid())
            self.assertEqual(len(summary.active_voice_problems), 1)
            rendered = format_summary(summary)
            self.assertIn("повреждённых строк: 0", rendered)
            self.assertIn("voice=2", rendered)
            self.assertIn("Проблемы активного голосового сеанса", rendered)

    def test_legacy_microphone_level_is_not_a_severity(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            log = runtime_dir / "logs" / "diagnostics.jsonl"
            log.parent.mkdir(parents=True)
            log.write_text(
                json.dumps(
                    {
                        "level": "1736",
                        "component": "stt",
                        "event": "worker_voice_started",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            summary = summarize(runtime_dir)
            self.assertEqual(summary.levels, {"info": 1})
            self.assertEqual(summary.legacy_audio_level_count, 1)
            self.assertIn("старого формата: 1", format_summary(summary))


if __name__ == "__main__":
    unittest.main()
