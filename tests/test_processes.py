import os
import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler.processes import (
    PROCESS_TERMINATE,
    SYNCHRONIZE,
    current_process_image_path,
    process_image_path,
    terminate_verified_process,
    OwnedProcessJob,
)


class ProcessIdentityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows job integration")
    def test_private_jobs_do_not_terminate_other_request(self):
        code = (
            "import os,time; from butler.processes import join_process_job; "
            "join_process_job(os.environ['KSENIA_BROWSER_JOB']); "
            "print('ready',flush=True); time.sleep(60)"
        )
        workers = []
        try:
            with OwnedProcessJob() as first, OwnedProcessJob() as second:
                for job in (first, second):
                    worker = subprocess.Popen(
                        [sys.executable, "-c", code], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                        env={**os.environ, "KSENIA_BROWSER_JOB": job.name},
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    workers.append(worker)
                    self.assertEqual(worker.stdout.readline().strip(), "ready")
                first.terminate()
                workers[0].wait(timeout=5)
                self.assertIsNone(workers[1].poll())
        finally:
            for worker in workers:
                worker.communicate(timeout=5)
                worker.stdout.close()
                worker.stderr.close()

    @unittest.skipUnless(os.name == "nt", "Windows job API")
    def test_job_api_failure_is_a_recoverable_os_error(self):
        with patch("win32job.CreateJobObject", side_effect=RuntimeError("API unavailable")):
            with self.assertRaises(OSError):
                with OwnedProcessJob():
                    self.fail("Failed job must not admit a worker")

    @unittest.skipUnless(os.name == "nt", "Windows job API")
    def test_job_accounting_failure_is_a_recoverable_os_error(self):
        with OwnedProcessJob() as job:
            with patch("win32job.QueryInformationJobObject", side_effect=RuntimeError("API unavailable")):
                with self.assertRaises(OSError):
                    job.terminate()

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
