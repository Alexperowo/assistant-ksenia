import socket
import sys
import threading
import unittest
from pathlib import Path

import numpy as np


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from audio_capture_service import FarReferenceBuffer, RenderReferenceServer  # noqa: E402
from audio_output import (  # noqa: E402
    FarReferencePublisher,
    PcmPlaybackController,
    ranked_output_devices,
)


class FakeSoundDevice:
    class PortAudioError(Exception):
        pass

    def __init__(self):
        self.default = type("Default", (), {"device": (-1, 2)})()
        self._devices = [
            {"name": "Display", "max_output_channels": 2, "hostapi": 0},
            {"name": "Headphones", "max_output_channels": 2, "hostapi": 1},
            {"name": "Headphones", "max_output_channels": 2, "hostapi": 2},
        ]
        self._hosts = [
            {"name": "Windows WASAPI"},
            {"name": "Windows DirectSound"},
            {"name": "MME"},
        ]

    def query_devices(self):
        return self._devices

    def query_hostapis(self, index):
        return self._hosts[index]


class AudioOutputTests(unittest.TestCase):
    def test_empty_selector_uses_exact_windows_default_output(self):
        candidates = ranked_output_devices(FakeSoundDevice(), "")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], 2)
        self.assertEqual(candidates[0][2], "MME")

    def test_explicit_name_can_choose_non_default_output(self):
        candidates = ranked_output_devices(FakeSoundDevice(), "Headphones")

        self.assertEqual([item[0] for item in candidates], [1, 2])

    def test_far_publisher_resamples_48k_mono_to_exact_16k_frame(self):
        token = "r" * 64
        reference = FarReferenceBuffer(frame_bytes=320)
        server = RenderReferenceServer(token, reference)
        try:
            server.start()
            publisher = FarReferencePublisher(
                "127.0.0.1", server.port, token, input_rate=48000
            )
            try:
                publisher.publish(np.arange(480, dtype=np.int16).tobytes())
                received = b""
                for _attempt in range(100):
                    received = reference.take()
                    if received != b"\x00" * 320:
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(len(received), 320)
            finally:
                publisher.close()
        finally:
            server.close()

    def test_far_publisher_refuses_non_loopback_host(self):
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            FarReferencePublisher(
                "192.0.2.1", 12345, "x" * 64, input_rate=48000
            )

    def test_pcm_stereo_expansion_preserves_each_mono_sample(self):
        mono = np.array([1, -2, 3], dtype=np.int16)
        stereo = np.frombuffer(
            PcmPlaybackController._stereo_frame(mono.tobytes(), 2),
            dtype=np.int16,
        ).reshape(-1, 2)

        self.assertTrue(np.array_equal(stereo[:, 0], mono))
        self.assertTrue(np.array_equal(stereo[:, 1], mono))


if __name__ == "__main__":
    unittest.main()
