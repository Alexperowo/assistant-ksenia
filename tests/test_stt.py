import json
import tempfile
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from butler.stt import SpeechRecognitionError, SpeechRecognizer


class SpeechRecognizerTests(unittest.TestCase):
    def test_partial_transcript_diagnostics_store_only_character_count(self):
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
                json.dumps(
                    {
                        "event": "partial_transcript",
                        "text": "личная команда",
                        "text_chars": 14,
                    }
                )
                + "\n"
            )

            with patch("butler.stt.diagnostic_event") as diagnostic:
                recognizer._read_service(worker)

            call = next(
                item
                for item in diagnostic.call_args_list
                if item.args[2] == "worker_partial_transcript"
            )
            self.assertEqual(call.kwargs["text_chars"], 14)
            self.assertNotIn("text", call.kwargs)

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

    def test_worker_progress_restores_trace_and_emits_first_partial_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                root=root,
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}, "voice": {}},
            )
            recognizer = SpeechRecognizer(settings)
            recognizer._request_trace_fields["17"] = {
                "trace_id": "trace-stt",
                "task_id": "task-stt",
            }
            worker = MagicMock()
            worker.stdout = io.StringIO(
                "\n".join(
                    json.dumps(item, ensure_ascii=False)
                    for item in (
                        {"event": "voice_started", "id": "17"},
                        {
                            "event": "partial_transcript",
                            "id": "17",
                            "text": "личная команда",
                            "text_chars": 14,
                        },
                        {
                            "event": "partial_transcript",
                            "id": "17",
                            "text": "личная команда целиком",
                            "text_chars": 21,
                        },
                        {
                            "event": "capture_completed",
                            "id": "17",
                            "endpoint_reason": "complete_transcript",
                        },
                    )
                )
                + "\n"
            )

            with (
                patch("butler.stt.diagnostic_event") as diagnostic,
                patch("butler.stt.diagnostic_milestone") as milestone,
            ):
                recognizer._read_service(worker)

            self.assertTrue(
                all(
                    call.kwargs["trace_id"] == "trace-stt"
                    for call in diagnostic.call_args_list
                )
            )
            names = [call.args[1] for call in milestone.call_args_list]
            self.assertEqual(
                names,
                ["voice_start", "stt_partial_first", "voice_end", "turn_detected"],
            )
            partial = next(
                call
                for call in milestone.call_args_list
                if call.args[1] == "stt_partial_first"
            )
            self.assertEqual(partial.kwargs["partial_transcript_chars"], 14)
            self.assertNotIn("text", partial.kwargs)

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
                {
                    "event": "semantic_endpointing_unavailable",
                    "id": "1",
                    "error_type": "RuntimeError",
                }
            )
            recognizer._events.put(
                {"event": "voice_started", "id": "1", "peak_level": 900}
            )
            recognizer._events.put(
                {
                    "event": "partial_transcript",
                    "id": "1",
                    "text": "проверь",
                    "text_chars": 7,
                }
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
            on_partial = MagicMock()

            with patch.object(recognizer, "_start_service", return_value=True):
                event = recognizer._listen_service(45, prompt, on_partial)

            self.assertEqual(event["text"], "проверка")
            prompt.assert_called_once_with()
            on_partial.assert_called_once_with("проверь")
            writes = "".join(call.args[0] for call in stdin.write.call_args_list)
            commands = [json.loads(line) for line in writes.splitlines()]
            self.assertEqual(commands[0]["cmd"], "prepare_listen")
            self.assertEqual(commands[1]["cmd"], "start_listen")
            recognizer._service = None

    def test_worker_exit_after_listen_started_does_not_trigger_second_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = SimpleNamespace(
                root=root,
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}, "voice": {}},
            )
            recognizer = SpeechRecognizer(settings)
            recognizer._service = MagicMock()
            recognizer._events.put({"event": "worker_exit", "returncode": 1})

            with patch.object(recognizer, "_start_service", return_value=True):
                with self.assertRaisesRegex(SpeechRecognitionError, "повторная запись"):
                    recognizer._listen_service(10)

            recognizer._service = None

    def test_semantic_endpointing_flags_are_confined_to_enabled_live_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_voice = {
                "python": str(root / "python.exe"),
                "wake_model": str(root / "wake-model"),
                "stt_model": str(root / "whisper-model"),
            }
            enabled = SpeechRecognizer(
                SimpleNamespace(
                    root=root,
                    runtime_dir=root / "runtime",
                    raw={
                        "diagnostics": {"enabled": False},
                        "voice": base_voice,
                        "live": {
                            "enabled": True,
                            "semantic_endpointing": True,
                            "turn_complete_silence_seconds": 0.4,
                            "turn_ordinary_silence_seconds": 0.9,
                            "turn_incomplete_silence_seconds": 2.4,
                        },
                    },
                )
            )
            disabled = SpeechRecognizer(
                SimpleNamespace(
                    root=root,
                    runtime_dir=root / "runtime",
                    raw={
                        "diagnostics": {"enabled": False},
                        "voice": base_voice,
                        "live": {"enabled": False, "semantic_endpointing": True},
                    },
                )
            )

            enabled_command = enabled._service_command()
            disabled_command = disabled._service_command()

            self.assertIn("--semantic-endpointing", enabled_command)
            self.assertIn("0.4", enabled_command)
            self.assertIn("0.9", enabled_command)
            self.assertIn("2.4", enabled_command)
            self.assertNotIn("--semantic-endpointing", disabled_command)


if __name__ == "__main__":
    unittest.main()
