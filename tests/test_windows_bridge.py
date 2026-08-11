import unittest

from butler.windows_bridge import active_window, list_windows


class WindowsBridgeTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(__import__("ctypes"), "windll"), "Только Windows")
    def test_lists_visible_windows(self):
        windows = list_windows()
        self.assertIsInstance(windows, list)

    @unittest.skipUnless(hasattr(__import__("ctypes"), "windll"), "Только Windows")
    def test_reads_active_window(self):
        self.assertIn("title", active_window())


if __name__ == "__main__":
    unittest.main()
