import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audio_devices import audio_inventory  # noqa: E402
from audio_input import (  # noqa: E402
    automatic_input_devices,
    open_best_input_stream,
    ranked_input_devices,
)
from wake_worker import is_activation  # noqa: E402


class FakeSoundDevice:
    class PortAudioError(Exception):
        pass

    def __init__(self):
        self._devices = [
            {"name": "Headset (JBL Sense Pro)", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 0, "default_samplerate": 16000},
            {"name": "Headset (JBL Sense Pro)", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 1, "default_samplerate": 16000},
            {"name": "Microphone", "max_input_channels": 1, "max_output_channels": 0, "hostapi": 2, "default_samplerate": 48000},
            {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 1, "default_samplerate": 48000},
        ]
        self._hosts = [
            {"name": "Windows WDM-KS"},
            {"name": "Windows WASAPI"},
            {"name": "MME"},
        ]
        self.default = type("Default", (), {"device": (2, 3)})()

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
        if device == 1:
            raise self.PortAudioError("endpoint busy")

    def RawInputStream(self, **_kwargs):
        return self._Stream()

    @staticmethod
    def WasapiSettings(**_kwargs):
        return object()


class NoDefaultSoundDevice(FallbackSoundDevice):
    def __init__(self):
        super().__init__()
        self.default = type("Default", (), {"device": (-1, -1)})()


class OneInputNoDefaultSoundDevice(NoDefaultSoundDevice):
    def __init__(self):
        super().__init__()
        self._devices = [self._devices[1]]

    def check_input_settings(self, **_kwargs):
        return None


class AudioInputTests(unittest.TestCase):
    def test_wasapi_is_preferred_over_wdm_ks(self):
        candidates = ranked_input_devices(FakeSoundDevice(), "JBL Sense Pro")
        self.assertEqual(candidates[0][2], "Windows WASAPI")
        self.assertEqual(candidates[-1][2], "Windows WDM-KS")

    def test_default_device_is_ranked_first_for_inventory(self):
        candidates = ranked_input_devices(FakeSoundDevice(), "")
        self.assertEqual(candidates[0][0], 2)
        self.assertEqual(len(candidates), 3)

    def test_audio_inventory_separates_inputs_outputs_and_defaults(self):
        inventory = audio_inventory(FakeSoundDevice())

        self.assertEqual(len(inventory["inputs"]), 3)
        self.assertEqual(len(inventory["outputs"]), 1)
        self.assertTrue(inventory["inputs"][2]["default"])
        self.assertTrue(inventory["outputs"][0]["default"])
        self.assertEqual(inventory["outputs"][0]["channels"], 2)

    def test_working_speech_default_is_selected_automatically(self):
        opened = open_best_input_stream(
            FallbackSoundDevice(),
            "",
            lambda *_args: None,
            target_rate=16000,
        )
        self.assertEqual(opened.device_name, "Headset (JBL Sense Pro)")
        self.assertEqual(opened.device_index, 0)

    def test_unknown_named_device_does_not_fallback_to_another_headset(self):
        candidates = ranked_input_devices(FakeSoundDevice(), "Unknown headset")
        self.assertEqual(candidates, [])

    def test_wake_phrase_accepts_common_recognition_variant(self):
        self.assertTrue(is_activation("сения слушать", "Ксения слушай"))
        self.assertFalse(is_activation("ксения", "Ксения слушай"))

    def test_selected_microphone_fallback_keeps_failed_host_api_attempts(self):
        opened = open_best_input_stream(
            FallbackSoundDevice(),
            "JBL Sense Pro",
            lambda *_args: None,
            target_rate=16000,
        )
        self.assertEqual(opened.device_index, 0)
        self.assertEqual(opened.candidate_count, 2)
        self.assertTrue(opened.failed_attempts)
        self.assertIn("endpoint busy", opened.failed_attempts[0])

    def test_without_speech_default_communication_device_beats_microphone_jack(self):
        opened = open_best_input_stream(
            NoDefaultSoundDevice(),
            "",
            lambda *_args: None,
            target_rate=16000,
        )
        self.assertEqual(opened.device_name, "Headset (JBL Sense Pro)")
        self.assertEqual(opened.device_index, 0)

    def test_loopback_and_line_sources_are_not_automatic_microphones(self):
        fake = NoDefaultSoundDevice()
        fake._devices = [
            {
                "name": "Stereo Mix",
                "max_input_channels": 2,
                "max_output_channels": 0,
                "hostapi": 2,
                "default_samplerate": 48000,
            },
            {
                "name": "Line Input",
                "max_input_channels": 2,
                "max_output_channels": 0,
                "hostapi": 2,
                "default_samplerate": 48000,
            },
        ]

        self.assertEqual(automatic_input_devices(fake), [])
        with self.assertRaisesRegex(RuntimeError, "речевой микрофон"):
            open_best_input_stream(fake, "", lambda *_args: None)

    def test_single_input_without_default_remains_automatic(self):
        opened = open_best_input_stream(
            OneInputNoDefaultSoundDevice(),
            "",
            lambda *_args: None,
            target_rate=16000,
        )
        self.assertEqual(opened.device_name, "Headset (JBL Sense Pro)")


if __name__ == "__main__":
    unittest.main()
