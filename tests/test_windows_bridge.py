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


if __name__ == "__main__":
    unittest.main()
