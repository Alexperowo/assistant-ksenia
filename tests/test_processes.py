import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler.processes import (
    PROCESS_TERMINATE,
    SYNCHRONIZE,
    current_process_image_path,
    process_image_path,
    terminate_verified_process,
)


class ProcessIdentityTests(unittest.TestCase):
    def test_verified_termination_waits_for_process_handles_to_close(self):
        expected = Path(sys.executable).resolve()
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.TerminateProcess.return_value = True
        kernel.WaitForSingleObject.return_value = 0
        with (
            patch("butler.processes.process_image_path", return_value=expected),
            patch("butler.processes._kernel32", return_value=kernel),
        ):
            self.assertTrue(terminate_verified_process(77, expected))

        kernel.OpenProcess.assert_called_once_with(
            PROCESS_TERMINATE | SYNCHRONIZE, False, 77
        )
        kernel.WaitForSingleObject.assert_called_once_with(123, 5_000)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_current_process_uses_real_image(self):
        actual = current_process_image_path()
        self.assertTrue(actual.is_file())
        detected = process_image_path(os.getpid())
        if detected is not None:
            self.assertEqual(actual, detected)
        else:
            self.assertEqual(actual, Path(sys.executable).resolve())


if __name__ == "__main__":
    unittest.main()
