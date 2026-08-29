import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from butler.speech import SpeechAnnouncer, SpeechCompletion, _PendingSpeech


class _CaptureInput:
    def __init__(self) -> None:
        self.value = ""
        self.closed = False

    def write(self, value: str) -> None:
        self.value += value

    def close(self) -> None:
        self.closed = True


class SpeechPrivacyTests(unittest.TestCase):
    def test_worker_ready_event_releases_startup_handshake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}},
            )
            announcer = SpeechAnnouncer(root, diagnostics_source=settings)
            worker = MagicMock()
            worker.stdout = io.StringIO(json.dumps({"event": "ready"}) + "\n")
            worker.poll.return_value = 0

            announcer._read_worker_events(worker)

            self.assertTrue(announcer._worker_ready.is_set())
            self.assertEqual(announcer._worker_start_error, "")

    def test_worker_startup_error_releases_handshake_and_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}},
            )
            announcer = SpeechAnnouncer(root, diagnostics_source=settings)
            worker = MagicMock()
            worker.stdout = io.StringIO(
                json.dumps({"event": "worker_error", "error": "bad model"}) + "\n"
            )
            worker.poll.return_value = 2

            announcer._read_worker_events(worker)

            self.assertTrue(announcer._worker_ready.is_set())
            self.assertEqual(announcer._worker_start_error, "bad model")

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
            announcer._pending["1"] = _PendingSpeech(
                waiter=waiter,
                original_text="проверка",
                spoken_text="проверка",
                result=result,
            )
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

    def test_tracked_speech_callback_waits_for_worker_completion(self):
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
            completions: list[SpeechCompletion] = []

            def accept(
                spoken_text: str,
                _speaker=None,
                *,
                wait=False,
                original_text=None,
                on_complete=None,
            ) -> bool:
                self.assertFalse(wait)
                announcer._pending["7"] = _PendingSpeech(
                    waiter=None,
                    original_text=str(original_text),
                    spoken_text=spoken_text,
                    result={},
                    on_complete=on_complete,
                )
                return True

            with patch.object(announcer, "_send_silero", side_effect=accept):
                accepted = announcer.say_tracked(
                    "Сегодня 10 августа.", completions.append
                )
            self.assertTrue(accepted)
            self.assertEqual(completions, [])

            worker = MagicMock()
            worker.stdout = io.StringIO(
                json.dumps({"event": "speech_done", "id": "7", "ok": True})
                + "\n"
            )
            worker.poll.return_value = 0
            announcer._read_worker_events(worker)

            self.assertEqual(len(completions), 1)
            self.assertTrue(completions[0].ok)
            self.assertFalse(completions[0].cancelled)
            self.assertEqual(completions[0].original_text, "Сегодня 10 августа.")
            self.assertNotRegex(completions[0].spoken_text, r"\d")

    def test_tracked_speech_never_falls_back_to_unordered_sapi(self):
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
            self.assertFalse(announcer.live_available())

            with (
                patch.object(announcer, "_send_silero", return_value=False),
                patch.object(announcer, "_speak_with_sapi") as sapi,
            ):
                accepted = announcer.say_tracked("Фраза.", lambda _result: None)

            self.assertFalse(accepted)
            sapi.assert_not_called()

    def test_tracked_worker_failure_reports_failure_without_sapi_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}},
            )
            announcer = SpeechAnnouncer(root, diagnostics_source=settings)
            completions: list[SpeechCompletion] = []
            announcer._pending["9"] = _PendingSpeech(
                waiter=None,
                original_text="Фраза.",
                spoken_text="Фраза.",
                result={},
                on_complete=completions.append,
            )
            worker = MagicMock()
            worker.stdout = io.StringIO(
                json.dumps(
                    {
                        "event": "speech_done",
                        "id": "9",
                        "ok": False,
                        "cancelled": False,
                    }
                )
                + "\n"
            )
            worker.poll.return_value = 0

            with patch.object(announcer, "_speak_with_sapi") as sapi:
                announcer._read_worker_events(worker)

            sapi.assert_not_called()
            self.assertEqual(len(completions), 1)
            self.assertFalse(completions[0].ok)
            self.assertFalse(completions[0].cancelled)

    def test_accepted_untracked_silero_request_is_never_repeated_by_late_sapi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}},
            )
            announcer = SpeechAnnouncer(root, diagnostics_source=settings)
            announcer._pending["10"] = _PendingSpeech(
                waiter=None,
                original_text="Фраза.",
                spoken_text="Фраза.",
                result={},
            )
            worker = MagicMock()
            worker.stdout = io.StringIO(
                json.dumps(
                    {
                        "event": "speech_done",
                        "id": "10",
                        "ok": False,
                        "cancelled": False,
                    }
                )
                + "\n"
            )
            worker.poll.return_value = 1

            with patch.object(announcer, "_speak_with_sapi") as sapi:
                announcer._read_worker_events(worker)

            sapi.assert_not_called()
            self.assertNotIn("10", announcer._pending)

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
