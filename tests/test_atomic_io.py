import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from butler.atomic_io import atomic_copy_file, atomic_write_text, exclusive_file_lock


class AtomicIoTests(unittest.TestCase):
    def test_lock_serializes_threads_for_the_same_target(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            state_lock = threading.Lock()
            active = 0
            maximum_active = 0

            def worker() -> None:
                nonlocal active, maximum_active
                with exclusive_file_lock(target):
                    with state_lock:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    time.sleep(0.005)
                    with state_lock:
                        active -= 1

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _index: worker(), range(24)))

        self.assertEqual(maximum_active, 1)

    def test_parallel_atomic_writes_leave_one_complete_value_and_no_temp_files(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            payloads = [f"payload-{index}-" + (str(index) * 1000) for index in range(24)]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda payload: atomic_write_text(target, payload), payloads))

            result = target.read_text(encoding="utf-8")
            leftovers = list(target.parent.glob(f".{target.name}.*.tmp"))

        self.assertIn(result, payloads)
        self.assertEqual(leftovers, [])

    def test_atomic_write_refuses_existing_symbolic_link(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "real.txt"
            target.write_text("original", encoding="utf-8")
            link = root / "link.txt"
            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaisesRegex(OSError, "символическую ссылку"):
                    atomic_write_text(link, "changed")
                with self.assertRaisesRegex(OSError, "символической ссылки"):
                    atomic_copy_file(link, root / "copy.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()
