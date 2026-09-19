import ctypes
import unittest

from butler.windows_bridge import WindowsBridgeError, _Input, active_window, list_windows


class WindowsBridgeTests(unittest.TestCase):
    def test_send_input_structure_matches_windows_abi(self):
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(_Input), expected)

    @unittest.skipUnless(hasattr(__import__("ctypes"), "windll"), "Только Windows")
    def test_lists_visible_windows(self):
        windows = list_windows()
        self.assertIsInstance(windows, list)

    @unittest.skipUnless(hasattr(__import__("ctypes"), "windll"), "Только Windows")
    def test_reads_active_window(self):
        try:
            info = active_window()
            self.assertIn("title", info)
        except WindowsBridgeError as exc:
            self.assertEqual(str(exc), "Активное окно не найдено.")

    def test_find_window_by_title_with_alias(self):
        from unittest.mock import patch
        from butler.windows_bridge import find_window_by_title

        mock_windows = [
            {"handle": 1234, "title": "Administrator: Windows PowerShell"},
            {"handle": 5678, "title": "Документ - Блокнот"},
        ]
        with patch("butler.windows_bridge.list_windows", return_value=mock_windows):
            self.assertEqual(find_window_by_title("терминал")["handle"], 1234)
            self.assertEqual(find_window_by_title("powershell")["handle"], 1234)
            self.assertEqual(find_window_by_title("блокнот")["handle"], 5678)
            self.assertIsNone(find_window_by_title("калькулятор"))
            self.assertIsNone(find_window_by_title(""))

    def test_manage_window_validates_action(self):
        from butler.windows_bridge import manage_window
        with self.assertRaises(WindowsBridgeError) as ctx:
            manage_window(handle=999999, action="unknown_action")
        self.assertIn("Неподдерживаемое действие", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
