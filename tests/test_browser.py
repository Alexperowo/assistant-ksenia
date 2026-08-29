import unittest
import json
import sys
import tempfile
import socket
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
            completed = __import__("subprocess").CompletedProcess(
                args=[], returncode=0, stdout=json.dumps({"results": []}), stderr=""
            )
            with patch("butler.browser.subprocess.run", return_value=completed) as run:
                reader.read("search", secret_query)
            command = run.call_args.args[0]
            self.assertNotIn(secret_query, command)
            self.assertIn("--value-stdin", command)
            self.assertEqual(run.call_args.kwargs["input"], secret_query)

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
