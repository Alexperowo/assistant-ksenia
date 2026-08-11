import json
import tempfile
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from butler.stt import SpeechRecognizer


class SpeechRecognizerTests(unittest.TestCase):
    def test_worker_audio_level_is_logged_as_signal_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                root=root,
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}, "voice": {}},
            )
            recognizer = SpeechRecognizer(settings)
            worker = MagicMock()
            worker.stdout = io.StringIO(
                json.dumps({"event": "voice_started", "level": 1736}) + "\n"
            )
            with patch("butler.stt.diagnostic_event") as diagnostic:
                recognizer._read_service(worker)
            call = next(
                item
                for item in diagnostic.call_args_list
                if item.args[2] == "worker_voice_started"
            )
            self.assertEqual(call.kwargs["signal_level"], 1736)
            self.assertNotIn("level", call.kwargs)

    def test_progress_events_do_not_finish_a_listen_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                root=root,
                runtime_dir=root / "runtime",
                raw={
                    "diagnostics": {"enabled": False},
                    "voice": {
                        "python": str(root / "python.exe"),
                        "wake_model": str(root / "wake-model"),
                        "stt_max_command_seconds": 45,
                    },
                },
            )
            recognizer = SpeechRecognizer(settings)
            stdin = MagicMock()
            recognizer._service = SimpleNamespace(stdin=stdin)
            recognizer._events.put({"event": "listening_ready", "id": "1"})
            recognizer._events.put({"event": "capture_started", "id": "1"})
            recognizer._events.put(
                {"event": "voice_started", "id": "1", "peak_level": 900}
            )
            recognizer._events.put(
                {"event": "capture_completed", "id": "1", "capture_seconds": 2.5}
            )
            recognizer._events.put(
                {
                    "event": "transcript",
                    "id": "1",
                    "text": "проверка",
                    "engine": "faster-whisper",
                    "capture_seconds": 2.5,
                }
            )
            prompt = MagicMock()

            with patch.object(recognizer, "_start_service", return_value=True):
                event = recognizer._listen_service(45, prompt)

            self.assertEqual(event["text"], "проверка")
            prompt.assert_called_once_with()
            writes = "".join(call.args[0] for call in stdin.write.call_args_list)
            commands = [json.loads(line) for line in writes.splitlines()]
            self.assertEqual(commands[0]["cmd"], "prepare_listen")
            self.assertEqual(commands[1]["cmd"], "start_listen")
            recognizer._service = None


if __name__ == "__main__":
    unittest.main()
