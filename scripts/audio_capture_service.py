from __future__ import annotations

import argparse
import hmac
import json
import os
import queue
import socket
import sys
import threading
from dataclasses import dataclass, field

from audio_input import TOKEN_ENVIRONMENT_KEY, open_best_input_stream
from pcm_audio import ratecv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Единый локальный захват микрофона")
    parser.add_argument("--device", default="")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frame-ms", type=int, default=10)
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
                    server.publish(frame)
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
        processor.start()
        print(
            json.dumps(
                {
                    "event": "ready",
                    "host": "127.0.0.1",
                    "port": server.port,
                    "frame_bytes": assembler.frame_bytes,
                    **metadata,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        for line in sys.stdin:
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
