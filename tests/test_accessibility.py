import unittest
from pathlib import Path

from butler.cli import _spoken_agent_error, _spoken_device_name, _spoken_microphone_error
from butler.confirmation import confirmation_text


class AccessibilityTests(unittest.TestCase):
    def test_microphone_errors_are_actionable_when_spoken(self):
        message = _spoken_microphone_error(RuntimeError("PortAudio device unavailable"))
        self.assertIn("Микрофон недоступен", message)
        self.assertIn("проверка микрофона", message)
        self.assertNotIn("показаны на экране", message)

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
