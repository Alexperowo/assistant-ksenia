import io
import json
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from butler.wake import WakeListener, WakeListenerCancelled, WakeListenerTimeout


class WakeListenerTests(unittest.TestCase):
    def _settings(self, root: Path):
        python = root / "python.exe"
        worker = root / "scripts" / "wake_worker.py"
        model = root / "wake-model"
        python.touch()
        worker.parent.mkdir()
        worker.touch()
        model.mkdir()
        return SimpleNamespace(
            root=root,
            runtime_dir=root / "runtime",
            raw={
                "diagnostics": {"enabled": False},
                "voice": {
                    "python": str(python),
                    "wake_model": str(model),
                    "wake_word": "Ксения слушай",
                },
            },
        )

    def test_ready_event_is_skipped_before_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self._settings(root)
            process = MagicMock()
            process.stdout = io.StringIO(
                json.dumps(
                    {
                        "event": "listening_ready",
                        "device": "Microphone",
                        "host_api": "WASAPI",
                    }
                )
                + "\n"
                + json.dumps({"event": "wake", "text": "ксения слушай"})
                + "\n"
            )
            process.stderr = io.StringIO("")
            process.poll.return_value = 0
            process.wait.return_value = 0
            with patch("butler.wake.subprocess.Popen", return_value=process):
                event = WakeListener(settings).wait_event(timeout=1)
            self.assertEqual(event["event"], "wake")

    def test_idle_timeout_has_a_distinct_nonfatal_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self._settings(root)
            process = MagicMock()
            process.stdout = io.StringIO(
                json.dumps(
                    {
                        "event": "timeout",
                        "error": "Фраза активации не распознана до истечения времени.",
                        "device": "Microphone",
                    }
                )
                + "\n"
            )
            process.stderr = io.StringIO("")
            process.poll.return_value = 0
            process.wait.return_value = 1
            with patch("butler.wake.subprocess.Popen", return_value=process):
                with self.assertRaises(WakeListenerTimeout):
                    WakeListener(settings).wait_event(timeout=1)

    def test_headset_event_can_activate_without_wake_phrase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self._settings(root)
            process = MagicMock()
            process.stdout = io.StringIO("")
            process.stderr = io.StringIO("")
            process.poll.return_value = None
            process.wait.return_value = 0
            buttons = queue.Queue()
            buttons.put(SimpleNamespace(name="play_pause", vk_code=0xB3))
            with patch("butler.wake.subprocess.Popen", return_value=process):
                event = WakeListener(settings).wait_event(
                    timeout=1, external_events=buttons
                )
            self.assertEqual(event["event"], "headset")
            self.assertEqual(event["button"], "play_pause")

    def test_pre_cancelled_listener_never_opens_microphone_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self._settings(root)
            cancelled = threading.Event()
            cancelled.set()
            with patch("butler.wake.subprocess.Popen") as popen:
                with self.assertRaises(WakeListenerCancelled):
                    WakeListener(settings).wait_event(
                        timeout=1, cancel_event=cancelled
                    )
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
