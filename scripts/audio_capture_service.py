from __future__ import annotations

import argparse
import ctypes
import hmac
import json
import os
import queue
import socket
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Iterator

from audio_input import TOKEN_ENVIRONMENT_KEY, open_best_input_stream
from pcm_audio import ratecv


WINDOWS_FILE_TYPE_PIPE = 0x0003
WINDOWS_BROKEN_PIPE_ERRORS = {109, 232}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Единый локальный захват микрофона")
    parser.add_argument("--device", default="")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frame-ms", type=int, default=10)
    parser.add_argument("--audio-processing", action="store_true")
    parser.add_argument("--stream-delay-ms", type=int, default=0)
    parser.add_argument("--ns-level", type=int, default=1)
    parser.add_argument("--auto-gain-control", action="store_true")
    return parser.parse_args()


class FrameAssembler:
    """Resample arbitrary input blocks and emit exact mono int16 frames."""

    def __init__(self, input_rate: int, output_rate: int, frame_ms: int) -> None:
        if input_rate <= 0 or output_rate <= 0 or frame_ms <= 0:
            raise ValueError("Частоты и размер кадра должны быть положительными.")
        frame_samples = output_rate * frame_ms
        if frame_samples % 1000:
            raise ValueError("Размер аудиокадра должен давать целое число samples.")
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.frame_bytes = frame_samples // 1000 * 2
        self._pending = bytearray()
        self._rate_state = None

    def feed(self, pcm: bytes) -> list[bytes]:
        if self.input_rate != self.output_rate:
            pcm, self._rate_state = ratecv(
                pcm,
                2,
                1,
                self.input_rate,
                self.output_rate,
                self._rate_state,
            )
        self._pending.extend(pcm)
        frames = []
        while len(self._pending) >= self.frame_bytes:
            frames.append(bytes(self._pending[: self.frame_bytes]))
            del self._pending[: self.frame_bytes]
        return frames


class FarReferenceBuffer:
    """Bounded render-reference queue consumed once per near-end frame."""

    def __init__(self, frame_bytes: int, maximum_frames: int = 400) -> None:
        if frame_bytes <= 0 or maximum_frames <= 0:
            raise ValueError("Размер far-end кадра и очереди должен быть положительным.")
        self.frame_bytes = frame_bytes
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=maximum_frames)
        self._silence = b"\x00" * frame_bytes
        self.dropped_frames = 0

    def publish(self, frame: bytes) -> None:
        if len(frame) != self.frame_bytes:
            raise ValueError("В far-end buffer передан кадр неверного размера.")
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
            self.dropped_frames += 1
            try:
                self._frames.put_nowait(frame)
            except queue.Full:
                self.dropped_frames += 1

    def take(self) -> bytes:
        try:
            return self._frames.get_nowait()
        except queue.Empty:
            return self._silence

    def clear(self) -> None:
        while True:
            try:
                self._frames.get_nowait()
            except queue.Empty:
                return


class AudioProcessingAdapter:
    """Small testable boundary around the optional WebRTC audio processor."""

    def __init__(self, processor: Any, far_reference: FarReferenceBuffer) -> None:
        self.processor = processor
        self.far_reference = far_reference

    def process(self, near_frame: bytes) -> bytes:
        if len(near_frame) != self.far_reference.frame_bytes:
            raise ValueError("Near-end кадр не совпадает с форматом AEC.")
        import numpy as np

        near = np.frombuffer(near_frame, dtype=np.int16)
        far = np.frombuffer(self.far_reference.take(), dtype=np.int16)
        result = self.processor.process(near, far)
        cleaned = result.tobytes()
        if len(cleaned) != len(near_frame):
            raise RuntimeError("AEC backend изменил размер PCM-кадра.")
        return cleaned


def _iter_control_lines(
    stream: BinaryIO,
    stopped: threading.Event,
    *,
    poll_interval: float = 0.05,
) -> Iterator[str]:
    """Read commands without a blocking Windows pipe read monopolising the GIL."""

    if os.name != "nt":
        for raw_line in stream:
            yield raw_line.decode("utf-8", errors="replace")
        return

    import msvcrt
    from ctypes import wintypes

    file_descriptor = stream.fileno()
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(file_descriptor))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_file_type = kernel32.GetFileType
    get_file_type.argtypes = [wintypes.HANDLE]
    get_file_type.restype = wintypes.DWORD
    if get_file_type(handle) != WINDOWS_FILE_TYPE_PIPE:
        for raw_line in stream:
            yield raw_line.decode("utf-8", errors="replace")
        return

    peek_named_pipe = kernel32.PeekNamedPipe
    peek_named_pipe.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    peek_named_pipe.restype = wintypes.BOOL
    pending = bytearray()
    while not stopped.wait(poll_interval):
        available = wintypes.DWORD()
        if not peek_named_pipe(handle, None, 0, None, ctypes.byref(available), None):
            error_code = ctypes.get_last_error()
            if error_code in WINDOWS_BROKEN_PIPE_ERRORS:
                break
            raise OSError(error_code, "Не удалось проверить канал управления аудиозахватом.")
        if available.value <= 0:
            continue
        chunk = os.read(file_descriptor, int(available.value))
        if not chunk:
            break
        pending.extend(chunk)
        while b"\n" in pending:
            raw_line, _, remainder = pending.partition(b"\n")
            pending = bytearray(remainder)
            yield raw_line.decode("utf-8", errors="replace") + "\n"
    if pending:
        yield pending.decode("utf-8", errors="replace")


@dataclass(eq=False)
class Subscriber:
    connection: socket.socket
    frames: queue.Queue[bytes] = field(default_factory=lambda: queue.Queue(maxsize=200))


class CaptureServer:
    def __init__(self, token: str, metadata: dict[str, object], frame_bytes: int) -> None:
        self.token = token
        self.metadata = metadata
        self.frame_bytes = frame_bytes
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(8)
        self.socket.settimeout(0.5)
        self.port = int(self.socket.getsockname()[1])
        self._stopped = threading.Event()
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._accept_thread = threading.Thread(target=self._accept_clients, daemon=True)

    def start(self) -> None:
        self._accept_thread.start()

    @staticmethod
    def _read_token(connection: socket.socket) -> str:
        received = bytearray()
        while len(received) <= 256:
            chunk = connection.recv(1)
            if not chunk or chunk == b"\n":
                break
            received.extend(chunk)
        return received.decode("ascii", errors="ignore")

    def _accept_clients(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._serve_client,
                args=(connection,),
                daemon=True,
            ).start()

    def _serve_client(self, connection: socket.socket) -> None:
        subscriber = None
        try:
            connection.settimeout(5)
            supplied = self._read_token(connection)
            if not hmac.compare_digest(supplied, self.token):
                connection.sendall(
                    json.dumps(
                        {"event": "error", "error": "Локальная подписка отклонена."},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                return
            subscriber = Subscriber(connection)
            with self._lock:
                self._subscribers.add(subscriber)
            header = {
                "event": "subscribed",
                "frame_bytes": self.frame_bytes,
                **self.metadata,
            }
            connection.sendall(
                json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            connection.settimeout(None)
            while not self._stopped.is_set():
                try:
                    frame = subscriber.frames.get(timeout=0.5)
                except queue.Empty:
                    continue
                connection.sendall(frame)
        except OSError:
            pass
        finally:
            if subscriber is not None:
                with self._lock:
                    self._subscribers.discard(subscriber)
            try:
                connection.close()
            except OSError:
                pass

    def publish(self, frame: bytes) -> None:
        if len(frame) != self.frame_bytes:
            raise ValueError("В ring buffer передан кадр неверного размера.")
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.frames.put_nowait(frame)
            except queue.Full:
                try:
                    subscriber.frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.frames.put_nowait(frame)
                except queue.Full:
                    pass

    def close(self) -> None:
        self._stopped.set()
        try:
            self.socket.close()
        except OSError:
            pass
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._accept_thread.join(timeout=2)


class RenderReferenceServer:
    """Accept exactly one authenticated far-end PCM publisher on loopback."""

    def __init__(self, token: str, reference: FarReferenceBuffer) -> None:
        self.token = token
        self.reference = reference
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(2)
        self.socket.settimeout(0.5)
        self.port = int(self.socket.getsockname()[1])
        self._stopped = threading.Event()
        self._active: socket.socket | None = None
        self._lock = threading.Lock()
        self._accept_thread = threading.Thread(target=self._accept_clients, daemon=True)

    def start(self) -> None:
        self._accept_thread.start()

    @staticmethod
    def _read_exact(connection: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def _accept_clients(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._serve_client,
                args=(connection,),
                daemon=True,
            ).start()

    def _serve_client(self, connection: socket.socket) -> None:
        accepted = False
        try:
            connection.settimeout(5)
            supplied = CaptureServer._read_token(connection)
            if not hmac.compare_digest(supplied, self.token):
                connection.sendall(
                    json.dumps(
                        {"event": "error", "error": "Far-end publisher отклонён."},
                        ensure_ascii=False,
                    ).encode("utf-8")
                    + b"\n"
                )
                return
            with self._lock:
                if self._active is not None:
                    connection.sendall(
                        json.dumps(
                            {"event": "error", "error": "Far-end publisher уже подключён."},
                            ensure_ascii=False,
                        ).encode("utf-8")
                        + b"\n"
                    )
                    return
                self._active = connection
                accepted = True
            connection.sendall(
                json.dumps(
                    {"event": "ready", "frame_bytes": self.reference.frame_bytes}
                ).encode("utf-8")
                + b"\n"
            )
            connection.settimeout(None)
            while not self._stopped.is_set():
                frame = self._read_exact(connection, self.reference.frame_bytes)
                if len(frame) != self.reference.frame_bytes:
                    return
                self.reference.publish(frame)
        except OSError as exc:
            if not self._stopped.is_set():
                print(
                    f"Render reference connection failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            print(
                f"Render reference processing failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        finally:
            if accepted:
                with self._lock:
                    if self._active is connection:
                        self._active = None
                self.reference.clear()
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stopped.set()
        try:
            self.socket.close()
        except OSError:
            pass
        with self._lock:
            active = self._active
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._accept_thread.join(timeout=2)


def main() -> int:
    args = parse_args()
    token = os.environ.get(TOKEN_ENVIRONMENT_KEY, "")
    if len(token) < 32:
        raise RuntimeError("Не задан ключ локального аудиозахвата.")
    import sounddevice as sd

    raw_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)

    def callback(indata, frames, callback_time, status) -> None:
        if status or not len(indata):
            return
        data = bytes(indata)
        try:
            raw_queue.put_nowait(data)
        except queue.Full:
            try:
                raw_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                raw_queue.put_nowait(data)
            except queue.Full:
                pass

    opened = open_best_input_stream(
        sd,
        args.device,
        callback,
        target_rate=args.sample_rate,
    )
    assembler = FrameAssembler(opened.sample_rate, args.sample_rate, args.frame_ms)
    far_reference = FarReferenceBuffer(assembler.frame_bytes)
    render_server = RenderReferenceServer(token, far_reference)
    audio_processor = None
    if args.audio_processing:
        from pywebrtc_audio import AudioProcessor

        processor = AudioProcessor(
            sample_rate=args.sample_rate,
            num_channels=1,
            echo_cancellation=True,
            noise_suppression=True,
            auto_gain_control=args.auto_gain_control,
            ns_level=args.ns_level,
            stream_delay_ms=args.stream_delay_ms,
        )
        audio_processor = AudioProcessingAdapter(processor, far_reference)
    metadata = {
        "sample_rate": args.sample_rate,
        "device_index": opened.device_index,
        "device_name": opened.device_name,
        "host_api": opened.host_api,
        "candidate_count": opened.candidate_count,
        "failed_attempts": list(opened.failed_attempts),
    }
    server = CaptureServer(token, metadata, assembler.frame_bytes)
    stopped = threading.Event()

    def process_audio() -> None:
        try:
            while not stopped.is_set():
                try:
                    chunk = raw_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                for frame in assembler.feed(chunk):
                    server.publish(
                        audio_processor.process(frame) if audio_processor else frame
                    )
        except Exception as exc:
            print(
                f"Audio capture processing failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            stopped.set()
            server.close()

    processor = threading.Thread(target=process_audio, daemon=True)
    try:
        server.start()
        render_server.start()
        processor.start()
        print(
            json.dumps(
                {
                    "event": "ready",
                    "host": "127.0.0.1",
                    "port": server.port,
                    "render_port": render_server.port,
                    "frame_bytes": assembler.frame_bytes,
                    "audio_processing": audio_processor is not None,
                    **metadata,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        for line in _iter_control_lines(sys.stdin.buffer, stopped):
            try:
                command = json.loads(line)
            except json.JSONDecodeError:
                continue
            if command.get("cmd") == "shutdown":
                break
        return 0
    finally:
        stopped.set()
        server.close()
        render_server.close()
        opened.close()
        processor.join(timeout=2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps({"event": "error", "error": str(exc)}, ensure_ascii=False),
            flush=True,
        )
        raise SystemExit(2)
