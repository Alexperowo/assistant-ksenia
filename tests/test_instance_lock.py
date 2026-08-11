import os
import tempfile
import unittest
from pathlib import Path

from butler.instance_lock import SingleInstance


@unittest.skipUnless(os.name == "nt", "Windows named mutex test")
class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_rejected_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with SingleInstance(root, "test") as first:
                self.assertTrue(first)
                with SingleInstance(root, "test") as second:
                    self.assertFalse(second)
            with SingleInstance(root, "test") as after_release:
                self.assertTrue(after_release)


if __name__ == "__main__":
    unittest.main()
