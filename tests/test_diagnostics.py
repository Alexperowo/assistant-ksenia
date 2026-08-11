import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from butler.diagnostics import event, safe_url, text_metadata


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


if __name__ == "__main__":
    unittest.main()
