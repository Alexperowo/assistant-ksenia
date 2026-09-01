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

from butler.wake import (
    MicrophoneCaptureGate,
    WakeListener,
    WakeListenerCancelled,
    WakeListenerTimeout,
)


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


class MicrophoneCaptureGateTests(unittest.TestCase):
    def test_exclusive_capture_waits_for_monitor_acknowledgement(self):
        gate = MicrophoneCaptureGate()
        owner_cancelled = threading.Event()
        monitor_cancel = gate.monitor_cancel_event(owner_cancelled)
        monitor_released = threading.Event()

        def monitor() -> None:
            while not monitor_cancel.is_set():
                owner_cancelled.wait(0.01)
            gate.monitor_checkpoint(owner_cancelled)
            monitor_released.set()

        thread = threading.Thread(target=monitor)
        thread.start()
        with gate.exclusive_capture(1):
            self.assertFalse(monitor_released.is_set())
        thread.join(1)
        self.assertTrue(monitor_released.is_set())

    def test_failed_handoff_releases_pause_request(self):
        gate = MicrophoneCaptureGate()
        owner_cancelled = threading.Event()
        with self.assertRaises(TimeoutError):
            with gate.exclusive_capture(0.01):
                self.fail("Захват не должен быть предоставлен без monitor acknowledgement.")
        self.assertFalse(gate.monitor_cancel_event(owner_cancelled).is_set())

    def test_any_owner_can_cancel_monitor(self):
        gate = MicrophoneCaptureGate()
        task_finished = threading.Event()
        streaming_started = threading.Event()
        combined = gate.monitor_cancel_event(task_finished, streaming_started)

        self.assertFalse(combined.is_set())
        streaming_started.set()
        self.assertTrue(combined.is_set())


if __name__ == "__main__":
    unittest.main()
