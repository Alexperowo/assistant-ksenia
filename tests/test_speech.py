import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from butler.speech import SpeechAnnouncer


class _CaptureInput:
    def __init__(self) -> None:
        self.value = ""
        self.closed = False

    def write(self, value: str) -> None:
        self.value += value

    def close(self) -> None:
        self.closed = True


class SpeechPrivacyTests(unittest.TestCase):
    def test_speech_started_is_progress_not_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}},
            )
            announcer = SpeechAnnouncer(root, diagnostics_source=settings)
            waiter = threading.Event()
            result = {}
            announcer._pending["1"] = (waiter, "проверка", result)
            worker = MagicMock()
            worker.stdout = io.StringIO(
                json.dumps({"event": "speech_started", "id": "1"})
                + "\n"
                + json.dumps(
                    {"event": "speech_done", "id": "1", "ok": True}
                )
                + "\n"
            )
            worker.poll.return_value = 0

            announcer._read_worker_events(worker)

            self.assertTrue(waiter.is_set())
            self.assertTrue(result["ok"])
            self.assertNotIn("1", announcer._pending)

    def test_speech_is_normalized_and_diagnostics_only_store_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "scripts" / "speak.ps1"
            script.parent.mkdir()
            script.touch()
            settings = SimpleNamespace(
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}},
            )
            announcer = SpeechAnnouncer(
                root,
                voice_config={"engine": "silero", "speaker": "xenia"},
                diagnostics_source=settings,
            )
            with (
                patch.object(announcer, "_send_silero", return_value=True) as send,
                patch("butler.speech.diagnostic_event") as diagnostic,
            ):
                announcer.say("Сегодня 10 августа 2026 года.")

            spoken = send.call_args.args[0]
            self.assertNotRegex(spoken, r"\d")
            self.assertIn("десятое августа", spoken)
            prepared = next(
                call for call in diagnostic.call_args_list if call.args[2] == "request_prepared"
            )
            self.assertEqual(prepared.kwargs["digit_count"], 6)
            self.assertTrue(prepared.kwargs["normalization_changed"])
            self.assertNotIn("text", prepared.kwargs)

    def test_sapi_text_is_sent_over_stdin_not_process_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}},
            )
            announcer = SpeechAnnouncer(
                root,
                voice_config={"engine": "sapi"},
                diagnostics_source=settings,
            )
            process = MagicMock()
            process.pid = 123
            process.stdin = _CaptureInput()
            process.stderr.read.return_value = ""
            process.wait.return_value = 0
            private_text = "личное сообщение Александра"
            with patch("butler.speech.subprocess.Popen", return_value=process) as popen:
                announcer._speak_with_sapi(private_text, wait=True)
            command = popen.call_args.args[0]
            self.assertNotIn(private_text, command)
            self.assertNotIn("-Text", command)
            self.assertEqual(process.stdin.value, private_text)
            self.assertTrue(process.stdin.closed)


if __name__ == "__main__":
    unittest.main()
