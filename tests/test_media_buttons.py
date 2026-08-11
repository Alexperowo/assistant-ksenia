import unittest
from types import SimpleNamespace

from butler.media_buttons import MediaButtonListener


class MediaButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(raw={"diagnostics": {"enabled": False}})

    def test_media_key_is_named_and_debounced(self):
        listener = MediaButtonListener(
            self.settings, buttons={"play_pause"}, debounce_ms=700
        )
        first = listener.record_virtual_key(0xB3, received_at=10.0)
        repeated = listener.record_virtual_key(0xB3, received_at=10.2)
        second = listener.record_virtual_key(0xB3, received_at=11.0)
        self.assertEqual(first.name, "play_pause")
        self.assertIsNone(repeated)
        self.assertEqual(second.name, "play_pause")

    def test_unconfigured_button_is_ignored(self):
        listener = MediaButtonListener(self.settings, buttons={"play_pause"})
        self.assertIsNone(listener.record_virtual_key(0xAF, received_at=10.0))


if __name__ == "__main__":
    unittest.main()
