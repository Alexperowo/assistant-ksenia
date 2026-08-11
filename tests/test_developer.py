import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from butler.developer import DeveloperError, DeveloperRunner


class DeveloperRunnerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
