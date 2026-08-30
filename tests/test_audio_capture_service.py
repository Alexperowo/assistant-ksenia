import os
import sys
import threading
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from audio_capture_service import (  # noqa: E402
    TOKEN_ENVIRONMENT_KEY as WORKER_TOKEN_ENVIRONMENT_KEY,
    CaptureServer,
    FrameAssembler,
)
from audio_input import open_remote_input_stream  # noqa: E402
from butler.audio_capture import CaptureEndpoint, TOKEN_ENVIRONMENT_KEY  # noqa: E402


class AudioCaptureServiceTests(unittest.TestCase):
    def test_frame_assembler_emits_exact_ten_millisecond_frames(self):
        assembler = FrameAssembler(16000, 16000, 10)
        self.assertEqual(assembler.frame_bytes, 320)
        self.assertEqual(assembler.feed(b"a" * 100), [])
        frames = assembler.feed(b"b" * 540)
        self.assertEqual([len(frame) for frame in frames], [320, 320])
        self.assertEqual(frames[0], b"a" * 100 + b"b" * 220)

    def test_capture_endpoint_keeps_token_out_of_process_arguments(self):
        endpoint = CaptureEndpoint(
            host="127.0.0.1",
            port=32123,
            token="a" * 64,
            sample_rate=16000,
            frame_bytes=320,
            device_name="Test microphone",
            host_api="Windows WASAPI",
        )

        arguments = endpoint.command_arguments()
        environment = endpoint.child_environment()

        self.assertNotIn(endpoint.token, arguments)
        self.assertNotIn(endpoint.token, repr(endpoint))
        self.assertEqual(environment[TOKEN_ENVIRONMENT_KEY], endpoint.token)
        self.assertNotEqual(os.environ.get(TOKEN_ENVIRONMENT_KEY), endpoint.token)
        self.assertEqual(TOKEN_ENVIRONMENT_KEY, WORKER_TOKEN_ENVIRONMENT_KEY)

    def test_authenticated_subscriber_receives_bounded_pcm_frame(self):
        token = "b" * 64
        metadata = {
            "sample_rate": 16000,
            "device_index": 7,
            "device_name": "Test microphone",
            "host_api": "Windows WASAPI",
            "candidate_count": 1,
            "failed_attempts": [],
        }
        server = CaptureServer(token, metadata, frame_bytes=320)
        received = []
        arrived = threading.Event()

        def callback(data, _frames, _callback_time, _status):
            received.append(bytes(data))
            arrived.set()

        try:
            server.start()
            opened = open_remote_input_stream(
                "127.0.0.1",
                server.port,
                token,
                callback,
            )
            try:
                frame = bytes(range(256)) + bytes(range(64))
                server.publish(frame)
                self.assertTrue(arrived.wait(2))
                self.assertEqual(received, [frame])
                self.assertEqual(opened.sample_rate, 16000)
                self.assertEqual(opened.device_name, "Test microphone")
                server.close()
                self.assertTrue(opened.stream.ended.wait(2))
            finally:
                opened.close()
        finally:
            server.close()

    def test_invalid_capture_token_is_rejected(self):
        server = CaptureServer(
            "c" * 64,
            {
                "sample_rate": 16000,
                "device_index": 1,
                "device_name": "Test microphone",
                "host_api": "Windows WASAPI",
                "candidate_count": 1,
                "failed_attempts": [],
            },
            frame_bytes=320,
        )
        try:
            server.start()
            with self.assertRaisesRegex(RuntimeError, "отклонена"):
                open_remote_input_stream(
                    "127.0.0.1",
                    server.port,
                    "d" * 64,
                    lambda *_args: None,
                )
        finally:
            server.close()

    def test_remote_capture_refuses_non_loopback_host(self):
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            open_remote_input_stream(
                "192.0.2.10",
                1234,
                "e" * 64,
                lambda *_args: None,
            )


if __name__ == "__main__":
    unittest.main()
