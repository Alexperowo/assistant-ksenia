import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audio_input import open_best_input_stream, ranked_input_devices  # noqa: E402
from wake_worker import is_activation  # noqa: E402


class FakeSoundDevice:
    class PortAudioError(Exception):
        pass

    def __init__(self):
        self._devices = [
            {"name": "Headset (JBL Sense Pro)", "max_input_channels": 1, "hostapi": 0},
            {"name": "Headset (JBL Sense Pro)", "max_input_channels": 1, "hostapi": 1},
            {"name": "Microphone", "max_input_channels": 1, "hostapi": 2},
        ]
        self._hosts = [
            {"name": "Windows WDM-KS"},
            {"name": "Windows WASAPI"},
            {"name": "MME"},
        ]
        self.default = type("Default", (), {"device": (2, -1)})()

    def query_devices(self, index=None):
        return self._devices if index is None else self._devices[index]

    def query_hostapis(self, index):
        return self._hosts[index]


class FallbackSoundDevice(FakeSoundDevice):
    class _Stream:
        def start(self):
            return None

        def stop(self):
            return None

        def close(self):
            return None

    def __init__(self):
        super().__init__()
        self.default = type("Default", (), {"device": (0, -1)})()
        for device in self._devices:
            device["default_samplerate"] = 16000

    def check_input_settings(self, *, device, **_kwargs):
        if device == 0:
            raise self.PortAudioError("endpoint busy")

    def RawInputStream(self, **_kwargs):
        return self._Stream()

    @staticmethod
    def WasapiSettings(**_kwargs):
        return object()


class AudioInputTests(unittest.TestCase):
    def test_wasapi_is_preferred_over_wdm_ks(self):
        candidates = ranked_input_devices(FakeSoundDevice(), "JBL Sense Pro")
        self.assertEqual(candidates[0][2], "Windows WASAPI")
        self.assertEqual(candidates[-1][2], "Windows WDM-KS")

    def test_default_device_is_first_but_other_devices_are_fallbacks(self):
        candidates = ranked_input_devices(FakeSoundDevice(), "")
        self.assertEqual(candidates[0][0], 2)
        self.assertEqual(len(candidates), 3)

    def test_wake_phrase_accepts_common_recognition_variant(self):
        self.assertTrue(is_activation("сения слушать", "Ксения слушай"))
        self.assertFalse(is_activation("ксения", "Ксения слушай"))

    def test_successful_fallback_keeps_failed_microphone_attempts(self):
        opened = open_best_input_stream(
            FallbackSoundDevice(), "", lambda *_args: None, target_rate=16000
        )
        self.assertEqual(opened.device_index, 1)
        self.assertEqual(opened.candidate_count, 3)
        self.assertTrue(opened.failed_attempts)
        self.assertIn("endpoint busy", opened.failed_attempts[0])


if __name__ == "__main__":
    unittest.main()
