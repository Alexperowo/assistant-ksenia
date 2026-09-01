import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from butler.cli import (
    _audio_devices,
    _spoken_agent_error,
    _spoken_device_name,
    _spoken_microphone_error,
)
from butler.confirmation import confirmation_text
from butler.stt import SpeechRecognitionError


class AccessibilityTests(unittest.TestCase):
    @patch("butler.cli.SpeechRecognizer")
    def test_device_inventory_explains_automatic_selection_with_multiple_inputs(
        self, recognizer_class
    ):
        recognizer_class.return_value.list_devices.return_value = [
            {
                "index": 4,
                "name": "Input A",
                "host_api": "Windows WASAPI",
                "sample_rate": 48000,
                "default": True,
            },
            {
                "index": 5,
                "name": "Input B",
                "host_api": "MME",
                "sample_rate": 44100,
                "default": False,
            },
        ]
        recognizer_class.return_value.list_output_devices.return_value = [
            {
                "index": 3,
                "name": "Speakers",
                "host_api": "Windows WASAPI",
                "sample_rate": 48000,
                "channels": 2,
                "default": True,
            }
        ]
        speech = MagicMock()
        settings = SimpleNamespace(raw={"voice": {}})

        with redirect_stdout(io.StringIO()) as output:
            result = _audio_devices(settings, speech)

        self.assertEqual(result, 0)
        self.assertIn("Автовыбор включён", output.getvalue())
        self.assertIn("Стереомикшер", output.getvalue())
        self.assertIn("Автовыбор речевого микрофона", speech.say_and_wait.call_args.args[0])

    @patch("butler.cli.SpeechRecognizer")
    def test_device_inventory_shows_persisted_selection_without_warning(
        self, recognizer_class
    ):
        recognizer_class.return_value.list_devices.return_value = [
            {
                "index": 1,
                "name": "Headset (JBL One)",
                "host_api": "Windows WASAPI",
                "sample_rate": 16000,
                "default": False,
            },
            {
                "index": 2,
                "name": "Microphone",
                "host_api": "MME",
                "sample_rate": 48000,
                "default": True,
            },
        ]
        recognizer_class.return_value.list_output_devices.return_value = [
            {
                "index": 3,
                "name": "Speakers",
                "host_api": "Windows WASAPI",
                "sample_rate": 48000,
                "channels": 2,
                "default": True,
            }
        ]
        speech = MagicMock()
        settings = SimpleNamespace(raw={"voice": {"wake_device": "JBL One"}})

        with redirect_stdout(io.StringIO()) as output:
            result = _audio_devices(settings, speech)

        self.assertEqual(result, 0)
        self.assertIn("Сохранённый выбор микрофона: JBL One", output.getvalue())
        self.assertNotIn("ВНИМАНИЕ", output.getvalue())

    @patch("butler.cli.SpeechRecognizer")
    def test_device_inventory_explains_current_windows_output_without_changing_it(
        self, recognizer_class
    ):
        recognizer_class.return_value.list_devices.return_value = [
            {
                "index": 1,
                "name": "Microphone",
                "host_api": "MME",
                "sample_rate": 48000,
                "default": True,
            }
        ]
        recognizer_class.return_value.list_output_devices.return_value = [
            {
                "index": 4,
                "name": "WCS Display",
                "host_api": "Windows WASAPI",
                "sample_rate": 44100,
                "channels": 2,
                "default": True,
            }
        ]
        speech = MagicMock()
        settings = SimpleNamespace(raw={"voice": {}})

        with redirect_stdout(io.StringIO()) as output:
            result = _audio_devices(settings, speech)

        self.assertEqual(result, 0)
        self.assertIn("Устройства вывода звука", output.getvalue())
        self.assertIn("системному маршруту Windows: WCS Display", output.getvalue())
        self.assertIn("пока не меняет устройство вывода", output.getvalue())
        self.assertIn("WCS Display", speech.say_and_wait.call_args.args[0])

    @patch("butler.cli.set_user_microphone")
    @patch("butler.cli.SpeechRecognizer")
    def test_device_selection_persists_name_fragment_not_portaudio_index(
        self, recognizer_class, save_microphone
    ):
        recognizer_class.return_value.list_devices.return_value = [
            {
                "index": 12,
                "name": "Головной телефон (2- JBL Tour One M3)",
                "host_api": "Windows WASAPI",
                "sample_rate": 16000,
                "default": False,
            },
            {
                "index": 19,
                "name": "Головной телефон (2- JBL Tour One M3)",
                "host_api": "Windows WDM-KS",
                "sample_rate": 16000,
                "default": False,
            },
        ]
        speech = MagicMock()
        settings = SimpleNamespace(root=Path("D:/Project/assistant-ksenia"))

        with redirect_stdout(io.StringIO()) as output:
            result = _audio_devices(
                settings,
                speech,
                select="JBL Tour One M3",
            )

        self.assertEqual(result, 0)
        save_microphone.assert_called_once_with(settings.root, "JBL Tour One M3")
        recognizer_class.return_value.probe_device.assert_called_once_with(
            "JBL Tour One M3"
        )
        self.assertNotIn("index", save_microphone.call_args.args)
        self.assertIn("Windows WASAPI", output.getvalue())

    @patch("butler.cli.set_user_microphone")
    @patch("butler.cli.SpeechRecognizer")
    def test_ambiguous_device_selection_fails_without_changing_config(
        self, recognizer_class, save_microphone
    ):
        recognizer_class.return_value.list_devices.return_value = [
            {
                "index": 1,
                "name": "JBL Sense Pro",
                "host_api": "Windows WASAPI",
                "sample_rate": 16000,
                "default": False,
            },
            {
                "index": 2,
                "name": "JBL Tour One M3",
                "host_api": "Windows WASAPI",
                "sample_rate": 16000,
                "default": False,
            },
        ]
        speech = MagicMock()

        with redirect_stdout(io.StringIO()) as output:
            result = _audio_devices(
                SimpleNamespace(root=Path("D:/Project/assistant-ksenia")),
                speech,
                select="JBL",
            )

        self.assertEqual(result, 1)
        save_microphone.assert_not_called()
        self.assertIn("неоднозначен", output.getvalue())

    @patch("butler.cli.set_user_microphone")
    @patch("butler.cli.SpeechRecognizer")
    def test_clearing_device_does_not_require_a_working_audio_runtime(
        self, recognizer_class, save_microphone
    ):
        speech = MagicMock()
        settings = SimpleNamespace(root=Path("D:/Project/assistant-ksenia"))

        with redirect_stdout(io.StringIO()):
            result = _audio_devices(settings, speech, clear=True)

        self.assertEqual(result, 0)
        recognizer_class.assert_not_called()
        save_microphone.assert_called_once_with(settings.root, "")

    @patch("butler.cli.set_user_microphone")
    @patch("builtins.input", return_value="Tour One M3")
    @patch("butler.cli.SpeechRecognizer")
    def test_interactive_device_selection_uses_name_fragment(
        self, recognizer_class, _input, save_microphone
    ):
        recognizer_class.return_value.list_devices.return_value = [
            {
                "index": 12,
                "name": "Головной телефон (2- JBL Tour One M3)",
                "host_api": "Windows WASAPI",
                "sample_rate": 16000,
                "default": False,
            }
        ]
        recognizer_class.return_value.probe_device.return_value = {
            "event": "probe_ready",
            "device": "Головной телефон (2- JBL Tour One M3)",
            "host_api": "Windows WASAPI",
            "sample_rate": 16000,
        }
        speech = MagicMock()
        settings = SimpleNamespace(root=Path("D:/Project/assistant-ksenia"))

        with redirect_stdout(io.StringIO()):
            result = _audio_devices(settings, speech, interactive=True)

        self.assertEqual(result, 0)
        save_microphone.assert_called_once_with(settings.root, "Tour One M3")

    @patch("butler.cli.set_user_microphone")
    @patch("butler.cli.SpeechRecognizer")
    def test_unavailable_microphone_is_not_persisted(
        self, recognizer_class, save_microphone
    ):
        recognizer_class.return_value.list_devices.return_value = [
            {
                "index": 12,
                "name": "JBL Tour One M3",
                "host_api": "Windows WDM-KS",
                "sample_rate": 16000,
                "default": False,
            }
        ]
        recognizer_class.return_value.probe_device.side_effect = SpeechRecognitionError(
            "endpoint unavailable"
        )
        speech = MagicMock()
        settings = SimpleNamespace(root=Path("D:/Project/assistant-ksenia"))

        with redirect_stdout(io.StringIO()) as output:
            result = _audio_devices(settings, speech, select="Tour One M3")

        self.assertEqual(result, 1)
        save_microphone.assert_not_called()
        self.assertIn("Настройка не изменена", output.getvalue())
        self.assertNotIn("endpoint unavailable", output.getvalue())

    def test_microphone_errors_are_actionable_when_spoken(self):
        message = _spoken_microphone_error(RuntimeError("PortAudio device unavailable"))
        self.assertIn("Микрофон недоступен", message)
        self.assertIn("проверка микрофона", message)
        self.assertNotIn("показаны на экране", message)

    def test_multiple_input_error_points_to_selection_shortcut(self):
        message = _spoken_microphone_error(
            RuntimeError("Выберите устройство через voice.wake_device")
        )
        self.assertIn("список микрофонов", message)
        self.assertIn("выберите", message)

    def test_missing_voice_python_points_to_audible_audit(self):
        message = _spoken_microphone_error(RuntimeError("Не найден голосовой Python"))
        self.assertIn("полный аудит", message)

    def test_parallel_task_error_is_actionable_when_spoken(self):
        message = _spoken_agent_error(RuntimeError("Ксения уже выполняет другую задачу."))
        self.assertIn("Дождитесь", message)
        self.assertIn("повторите", message)

    def test_windows_driver_noise_is_removed_from_spoken_device_name(self):
        raw = "Headset (@System32\\drivers\\bthhfenum.sys; (JBL Sense Pro))"
        self.assertEqual(_spoken_device_name(raw), "JBL Sense Pro")

    def test_confirmation_does_not_speak_or_display_secrets(self):
        typed = confirmation_text(
            "windows_type_text", {"text": "super-secret-password"}
        )
        web = confirmation_text(
            "browser_interact",
            {
                "url": "https://example.test/login?token=very-secret#private",
                "actions": [{"fill": "another-secret"}],
            },
        )
        command = confirmation_text(
            "run_project_command",
            {"command": ["python.exe", "audit.py", "--token", "secret"]},
        )

        self.assertIn("21 символ", typed)
        self.assertNotIn("secret", typed)
        self.assertIn("example.test/login", web)
        self.assertNotIn("token", web)
        self.assertNotIn("private", web)
        self.assertNotIn("another-secret", web)
        self.assertIn("python.exe", command)
        self.assertIn("audit.py", command)
        self.assertNotIn("--token", command)
        self.assertNotIn("secret", command)

    def test_lan_page_has_keyboard_and_screen_reader_support(self):
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('aria-keyshortcuts="Control+Enter"', html)
        self.assertIn("if (!confirmationWasVisible) approve.focus()", html)
        self.assertIn("task.confirmation.message", html)
        self.assertNotIn("JSON.stringify(task.confirmation.arguments", html)
        self.assertIn("Сохранённый PIN не подошёл", html)
        self.assertIn("pinInput.focus()", html)


if __name__ == "__main__":
    unittest.main()
