import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from butler.developer import DeveloperError, DeveloperRunner


class DeveloperRunnerTests(unittest.TestCase):
    @staticmethod
    def settings(directory: str, developer: dict[str, object]):
        return SimpleNamespace(
            root=Path(directory).resolve(),
            raw={"developer": developer},
        )

    def test_denies_unknown_program(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                root=Path(directory).resolve(),
                raw={"developer": {"allowed_programs": ["python"]}},
            )
            with self.assertRaises(DeveloperError):
                DeveloperRunner(settings).run(["powershell.exe", "anything"])

    def test_denies_working_directory_outside_project(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                root=Path(directory).resolve(),
                raw={"developer": {"allowed_programs": ["python"]}},
            )
            with self.assertRaises(DeveloperError):
                DeveloperRunner(settings).run(["python", "--version"], Path(directory).parent)

    @patch("butler.developer.subprocess.run")
    def test_execution_is_disabled_by_default(self, run):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory, {"allowed_programs": ["python"]})

            with self.assertRaisesRegex(DeveloperError, "OS-песочницы"):
                DeveloperRunner(settings).run(["python", "--version"])

        run.assert_not_called()

    @patch("butler.developer.subprocess.run")
    def test_unsafe_host_requires_exact_risk_acknowledgement(self, run):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(
                directory,
                {
                    "allowed_programs": ["python"],
                    "execution": {
                        "backend": "unsafe_host",
                        "unsafe_host_acknowledgement": "yes",
                    },
                },
            )

            with self.assertRaisesRegex(DeveloperError, "подтверждение риска"):
                DeveloperRunner(settings).run(["python", "--version"])

        run.assert_not_called()

    @patch("butler.developer.subprocess.run")
    def test_unknown_backend_fails_closed(self, run):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(
                directory,
                {
                    "allowed_programs": ["python"],
                    "execution": {"backend": "mystery"},
                },
            )

            with self.assertRaisesRegex(DeveloperError, "Неизвестный backend"):
                DeveloperRunner(settings).run(["python", "--version"])

        run.assert_not_called()

    @patch("butler.developer.subprocess.run")
    def test_explicit_unsafe_host_keeps_shell_disabled(self, run):
        run.return_value = SimpleNamespace(stdout="Python 3.12\n", returncode=0)
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(
                directory,
                {
                    "allowed_programs": ["python"],
                    "execution": {
                        "backend": "unsafe_host",
                        "unsafe_host_acknowledgement": (
                            "I_ACCEPT_CODE_EXECUTION_AS_WINDOWS_USER"
                        ),
                    },
                },
            )

            result = DeveloperRunner(settings).run(["python", "--version"])

        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.output, "Python 3.12\n")
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["timeout"], 180)


if __name__ == "__main__":
    unittest.main()
