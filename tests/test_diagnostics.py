import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from butler.diagnostics import (
    bind_trace_context,
    current_trace_fields,
    event,
    new_trace_id,
    safe_url,
    text_metadata,
    trace_scope,
)


class DiagnosticsTests(unittest.TestCase):
    def _settings(self, runtime_dir: Path, **diagnostics):
        return SimpleNamespace(
            runtime_dir=runtime_dir,
            raw={
                "diagnostics": {
                    "enabled": True,
                    "allow_during_tests": True,
                    **diagnostics,
                }
            },
        )

    def test_sensitive_content_and_url_parameters_are_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            event(
                settings,
                "test",
                "private",
                prompt="совершенно секретный вопрос",
                token="secret-token-123",
                url="https://example.test/path?secret=yes#fragment",
                error="Bearer abcdef and password=hunter2",
            )
            raw = (Path(directory) / "logs" / "diagnostics.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("совершенно секретный", raw)
            self.assertNotIn("secret-token-123", raw)
            self.assertNotIn("secret=yes", raw)
            self.assertNotIn("abcdef", raw)
            self.assertNotIn("hunter2", raw)
            saved = json.loads(raw)
            self.assertTrue(saved["prompt"]["redacted"])
            self.assertEqual(saved["url"], "https://example.test/path")

    def test_parallel_writes_are_complete_json_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            threads = [
                threading.Thread(
                    target=lambda base=index: [
                        event(settings, "thread", "tick", item=base * 20 + offset)
                        for offset in range(20)
                    ]
                )
                for index in range(5)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            lines = (Path(directory) / "logs" / "diagnostics.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(lines), 100)
            self.assertTrue(all(json.loads(line)["event"] == "tick" for line in lines))

    def test_rotation_bounds_the_active_file(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(
                Path(directory), max_file_bytes=700, backup_count=2
            )
            for index in range(30):
                event(settings, "rotation", "line", index=index, detail="x" * 80)
            logs = Path(directory) / "logs"
            self.assertTrue((logs / "diagnostics.jsonl.1").is_file())
            self.assertLess((logs / "diagnostics.jsonl").stat().st_size, 1000)
            self.assertFalse((logs / "diagnostics.jsonl.3").exists())

    def test_helpers_return_stable_metadata_and_safe_url(self):
        self.assertEqual(text_metadata("abc"), text_metadata("abc"))
        self.assertEqual(text_metadata("abc")["length"], 3)
        self.assertEqual(
            safe_url("https://example.test/a?q=1#x"), "https://example.test/a"
        )

    def test_caller_cannot_overwrite_event_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            event(settings, "real", "identity", pid=999999, session_id="forged")
            saved = json.loads(
                (Path(directory) / "logs" / "diagnostics.jsonl").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved["component"], "real")
            self.assertEqual(saved["event"], "identity")
            self.assertEqual(saved["pid"], os.getpid())
            self.assertEqual(saved["reported_pid"], 999999)
            self.assertEqual(saved["reported_session_id"], "forged")

    def test_trace_scope_is_nested_and_does_not_leak(self):
        self.assertEqual(current_trace_fields(), {})
        trace_id = new_trace_id()
        with trace_scope(trace_id=trace_id, turn_id=7, ignored="value"):
            self.assertEqual(
                current_trace_fields(),
                {"trace_id": trace_id, "turn_id": "7"},
            )
            with trace_scope(task_id="task-1"):
                self.assertEqual(
                    current_trace_fields(),
                    {
                        "trace_id": trace_id,
                        "turn_id": "7",
                        "task_id": "task-1",
                    },
                )
            self.assertNotIn("task_id", current_trace_fields())
        self.assertEqual(current_trace_fields(), {})

    def test_trace_fields_are_written_but_cannot_replace_event_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with trace_scope(trace_id="trace-1", task_id="task-1"):
                event(settings, "test", "traced", trace_id="trace-explicit")
            saved = json.loads(
                (Path(directory) / "logs" / "diagnostics.jsonl").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(saved["trace_id"], "trace-explicit")
            self.assertEqual(saved["task_id"], "task-1")
            self.assertIsInstance(saved["monotonic_ns"], int)

    def test_trace_snapshot_can_be_bound_to_a_background_thread(self):
        observed = []
        with trace_scope(trace_id="trace-thread", task_id="task-thread"):
            target = bind_trace_context(
                lambda: observed.append(current_trace_fields())
            )
        thread = threading.Thread(target=target)
        thread.start()
        thread.join()
        self.assertEqual(
            observed,
            [{"trace_id": "trace-thread", "task_id": "task-thread"}],
        )


if __name__ == "__main__":
    unittest.main()
