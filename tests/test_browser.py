import unittest
import json
import sys
import tempfile
import socket
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler.browser import (
    BrowserError,
    BrowserReader,
    contains_financial_action,
    public_http_url,
)
from butler.config import load_settings


class BrowserSafetyTests(unittest.TestCase):
    def test_worker_environment_has_no_machine_specific_browser_path(self):
        reader = BrowserReader(load_settings())
        with patch.dict("os.environ", {}, clear=True):
            environment = reader._environment()
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", environment)
        self.assertEqual(environment["PYTHONUTF8"], "1")

    def test_local_private_and_file_urls_are_rejected(self):
        self.assertFalse(public_http_url("file:///C:/Users/Example/private.txt"))
        self.assertFalse(public_http_url("http://127.0.0.1:18080/health"))
        self.assertFalse(public_http_url("http://192.168.0.1/"))
        self.assertFalse(public_http_url("http://localhost:8765/"))
        self.assertFalse(public_http_url("http://2130706433:8765/"))
        self.assertFalse(public_http_url("http://127.1:8765/"))
        self.assertFalse(public_http_url("https://user:secret@example.com/"))
        with patch(
            "butler.browser.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            self.assertTrue(public_http_url("https://example.com/product"))

    def test_domain_resolving_to_private_address_is_rejected(self):
        with patch(
            "butler.browser.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
            ],
        ):
            self.assertFalse(public_http_url("https://public-looking.example/"))

    def test_read_only_modes_do_not_use_authenticated_persistent_profile(self):
        reader = object.__new__(BrowserReader)
        reader.persistent = True
        reader.settings = SimpleNamespace(raw={"diagnostics": {"enabled": False}})
        reader._validate = lambda _mode: None
        reader._read_persistent = Mock(return_value={"unexpected": True})
        reader._read_once = Mock(return_value={"results": []})

        result = reader.read("search", "безопасный запрос")

        self.assertEqual(result, {"results": []})
        reader._read_persistent.assert_not_called()
        reader._read_once.assert_called_once()

    def test_request_value_is_sent_over_stdin_not_process_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "chrome.exe"
            worker = root / "browser_worker.py"
            executable.touch()
            worker.touch()
            reader = object.__new__(BrowserReader)
            reader.settings = SimpleNamespace(
                runtime_dir=root / "runtime",
                raw={"diagnostics": {"enabled": False}},
            )
            reader.python = Path(sys.executable)
            reader.executable = executable
            reader.profile_dir = root / "profile"
            reader.headless = True
            reader.worker = worker
            reader.timeout = 10
            reader.max_text = 1000
            reader.persistent = False
            secret_query = "очень личный поисковый запрос"
            process = Mock(returncode=0, args=[])
            process.poll.return_value = 0
            process.communicate.return_value = (json.dumps({"results": []}), "")
            with (
                patch("butler.browser.OwnedProcessJob"),
                patch("butler.browser.subprocess.Popen", return_value=process) as run,
            ):
                reader.read("search", secret_query)
            command = run.call_args.args[0]
            self.assertNotIn(secret_query, command)
            self.assertIn("--value-stdin", command)
            self.assertEqual(process.communicate.call_args_list[0].kwargs["input"], secret_query)

    def assert_worker_tree_reaped(self, outcome):
        from butler.tasking import TaskCancelled
        from butler.processes import process_image_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            worker = scripts / "worker.py"
            marker = root / "child.pid"
            # A real cooperating worker and child, but no network or user browser.
            worker.write_text(
                "import os,sys,subprocess,time\n"
                "from pathlib import Path\n"
                "from butler.processes import join_process_job,current_process_image_path\n"
                "join_process_job(os.environ['KSENIA_BROWSER_JOB'])\n"
                "sys.stdin.read()\n"
                "child=subprocess.Popen([str(current_process_image_path()),'-c','import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                "Path('child.pid').write_text(str(child.pid))\n"
                + ("print('{}')\n" if outcome == "success" else "time.sleep(60)\n"),
                encoding="utf-8",
            )
            reader = object.__new__(BrowserReader)
            reader.settings = SimpleNamespace(raw={"diagnostics": {"enabled": False}})
            reader.python = Path(sys.executable)
            reader.executable = Path(sys.executable)
            reader.profile_dir = root / "profile"
            reader.headless = True
            reader.worker = worker
            reader.timeout = 3 if outcome == "timeout" else 10
            reader.max_text = 100
            child_pid = []

            def checkpoint():
                if marker.exists():
                    text = marker.read_text()
                    if text:
                        child_pid.append(int(text))
                        if outcome == "cancel":
                            raise TaskCancelled("Cancel owned request")

            if outcome == "success":
                self.assertEqual(reader._read_once("search", "query", checkpoint=checkpoint), {})
            else:
                with self.assertRaises(TaskCancelled if outcome == "cancel" else BrowserError):
                    reader._read_once("search", "query", checkpoint=checkpoint)
            if not child_pid and marker.exists():
                child_pid.append(int(marker.read_text()))
            self.assertTrue(child_pid)
            self.assertIsNone(process_image_path(child_pid[0]))

    @unittest.skipUnless(os.name == "nt", "Windows process-tree integration")
    def test_cancelled_request_reaps_worker_and_descendant(self):
        self.assert_worker_tree_reaped("cancel")

    @unittest.skipUnless(os.name == "nt", "Windows process-tree integration")
    def test_timed_out_request_reaps_worker_and_descendant(self):
        self.assert_worker_tree_reaped("timeout")

    @unittest.skipUnless(os.name == "nt", "Windows process-tree integration")
    def test_completed_request_reaps_leftover_descendant(self):
        self.assert_worker_tree_reaped("success")

    def test_tree_setup_failure_does_not_launch_unmanaged_worker(self):
        reader = object.__new__(BrowserReader)
        with (
            patch("butler.browser.OwnedProcessJob", side_effect=OSError("Job unavailable")),
            patch("butler.browser.subprocess.Popen") as launch,
        ):
            with self.assertRaises(BrowserError):
                reader._read_once("search", "query")
        launch.assert_not_called()

    def test_send_button_is_not_a_normal_browser_action(self):
        risk = BrowserReader._action_risk(
            "https://mail.example.test/inbox",
            [{"type": "click_text", "text": "Отправить"}],
        )
        self.assertEqual(risk, "send_message")

    def test_enter_on_messenger_is_a_send_action(self):
        risk = BrowserReader._action_risk(
            "https://web.telegram.org/",
            [{"type": "press", "selector": "textarea", "key": "Enter"}],
        )
        self.assertEqual(risk, "send_message")

    def test_financial_action_is_detected_before_send(self):
        risk = BrowserReader._action_risk(
            "https://shop.example.test/cart",
            [{"type": "click_text", "text": "Оплатить"}],
        )
        self.assertEqual(risk, "financial")
        self.assertTrue(contains_financial_action("Оформить заказ"))
        self.assertTrue(contains_financial_action("Bank transfer"))

    def test_send_requires_explicit_send_method(self):
        reader = object.__new__(BrowserReader)
        with self.assertRaises(BrowserError):
            reader.interact(
                "https://mail.example.test/",
                [{"type": "click_text", "text": "Отправить"}],
            )


if __name__ == "__main__":
    unittest.main()
