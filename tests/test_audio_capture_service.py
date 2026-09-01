import os
import socket
import sys
import threading
import unittest
from pathlib import Path

import numpy as np


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from audio_capture_service import (  # noqa: E402
    TOKEN_ENVIRONMENT_KEY as WORKER_TOKEN_ENVIRONMENT_KEY,
    AudioProcessingAdapter,
    CaptureServer,
    FarReferenceBuffer,
    FrameAssembler,
    RenderReferenceServer,
    _iter_control_lines,
)
from audio_input import open_remote_input_stream  # noqa: E402
from butler.audio_capture import CaptureEndpoint, TOKEN_ENVIRONMENT_KEY  # noqa: E402


class AudioCaptureServiceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows named-pipe regression")
    def test_control_pipe_wait_does_not_block_other_python_threads(self):
        read_descriptor, write_descriptor = os.pipe()
        stopped = threading.Event()
        received = []
        heartbeat = threading.Event()
        try:
            with os.fdopen(read_descriptor, "rb", buffering=0) as reader:
                consumer = threading.Thread(
                    target=lambda: received.extend(
                        _iter_control_lines(reader, stopped, poll_interval=0.01)
                    ),
                    daemon=True,
                )
                consumer.start()
                threading.Timer(0.05, heartbeat.set).start()
                self.assertTrue(heartbeat.wait(1))
                os.write(write_descriptor, b'{"cmd":"shutdown"}\n')
                os.close(write_descriptor)
                write_descriptor = -1
                consumer.join(timeout=2)
                self.assertFalse(consumer.is_alive())
                self.assertEqual(received, ['{"cmd":"shutdown"}\n'])
        finally:
            stopped.set()
            if write_descriptor >= 0:
                os.close(write_descriptor)

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
            render_port=32124,
        )

        arguments = endpoint.command_arguments()
        environment = endpoint.child_environment()

        self.assertNotIn(endpoint.token, arguments)
        self.assertNotIn(endpoint.token, repr(endpoint))
        self.assertEqual(environment[TOKEN_ENVIRONMENT_KEY], endpoint.token)
        self.assertNotEqual(os.environ.get(TOKEN_ENVIRONMENT_KEY), endpoint.token)
        self.assertEqual(TOKEN_ENVIRONMENT_KEY, WORKER_TOKEN_ENVIRONMENT_KEY)

    def test_authenticated_render_publisher_delivers_exact_far_frame(self):
        token = "f" * 64
        reference = FarReferenceBuffer(frame_bytes=320, maximum_frames=2)
        server = RenderReferenceServer(token, reference)
        try:
            server.start()
            connection = socket.create_connection(("127.0.0.1", server.port), timeout=2)
            try:
                connection.sendall(token.encode("ascii") + b"\n")
                header = bytearray()
                while not header.endswith(b"\n"):
                    header.extend(connection.recv(1))
                self.assertIn(b'"event": "ready"', bytes(header))
                frame = bytes(range(256)) + bytes(range(64))
                connection.sendall(frame)
                for _attempt in range(100):
                    received = reference.take()
                    if received == frame:
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(received, frame)
            finally:
                connection.close()
        finally:
            server.close()

    def test_audio_processing_pairs_near_with_far_and_preserves_frame_size(self):
        class FakeProcessor:
            def __init__(self):
                self.calls = []

            def process(self, near, far):
                self.calls.append((near.copy(), far.copy()))
                return near - far

        reference = FarReferenceBuffer(frame_bytes=320)
        far = np.full(160, 2, dtype=np.int16)
        near = np.full(160, 7, dtype=np.int16)
        reference.publish(far.tobytes())
        processor = FakeProcessor()
        adapter = AudioProcessingAdapter(processor, reference)

        cleaned = np.frombuffer(adapter.process(near.tobytes()), dtype=np.int16)

        self.assertTrue(np.all(cleaned == 5))
        self.assertTrue(np.array_equal(processor.calls[0][0], near))
        self.assertTrue(np.array_equal(processor.calls[0][1], far))

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
