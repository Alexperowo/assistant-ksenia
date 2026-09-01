from __future__ import annotations

import json
import socket
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pcm_audio import ratecv


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _host_api_name(sd, info: dict[str, Any]) -> str:
    try:
        return str(sd.query_hostapis(int(info.get("hostapi", -1))).get("name", ""))
    except (TypeError, ValueError, sd.PortAudioError):
        return ""


def _default_output_index(sd) -> int | None:
    try:
        index = int(sd.default.device[1])
    except (AttributeError, IndexError, TypeError, ValueError, sd.PortAudioError):
        return None
    return index if index >= 0 else None


def ranked_output_devices(sd, selector: str) -> list[tuple[int, dict[str, Any], str]]:
    """Resolve an explicit output selector or the exact Windows default route."""
    selector = selector.strip()
    default_index = _default_output_index(sd) if not selector else None
    try:
        numeric_selector = int(selector) if selector else None
    except ValueError:
        numeric_selector = None
    needle = selector.casefold()
    candidates = []
    host_priority = {
        "windows wasapi": 0,
        "windows directsound": 10,
        "mme": 20,
        "windows wdm-ks": 100,
    }
    for index, raw_info in enumerate(sd.query_devices()):
        info = dict(raw_info)
        if int(info.get("max_output_channels", 0)) < 1:
            continue
        if default_index is not None and index != default_index:
            continue
        name = str(info.get("name", ""))
        if numeric_selector is not None and index != numeric_selector:
            continue
        if needle and numeric_selector is None and needle not in name.casefold():
            continue
        host = _host_api_name(sd, info)
        score = host_priority.get(host.casefold(), 50)
        if needle and name.casefold() == needle:
            score -= 5
        candidates.append((score, index, info, host))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [(index, info, host) for _score, index, info, host in candidates]


@dataclass(frozen=True)
class OutputRoute:
    device_index: int
    device_name: str
    host_api: str
    sample_rate: int
    channels: int


class FarReferencePublisher:
    """Authenticated loopback publisher for mono 16-bit 10 ms render frames."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        input_rate: int,
        output_rate: int = 16000,
        timeout: float = 5.0,
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise RuntimeError("Far-end reference разрешён только через loopback.")
        if port <= 0 or len(token) < 32:
            raise RuntimeError("Повреждён endpoint far-end reference.")
        self.input_rate = int(input_rate)
        self.output_rate = int(output_rate)
        self.frame_bytes = self.output_rate // 100 * 2
        self._state = None
        self._pending = bytearray()
        try:
            connection = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as exc:
            raise RuntimeError(
                f"Не удалось подключиться к far-end service: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            connection.sendall(token.encode("ascii") + b"\n")
            header = bytearray()
            try:
                while len(header) <= 8192:
                    chunk = connection.recv(1)
                    if not chunk or chunk == b"\n":
                        break
                    header.extend(chunk)
            except TimeoutError as exc:
                raise RuntimeError(
                    "Far-end service не ответил на аутентификацию вовремя."
                ) from exc
            value = json.loads(header.decode("utf-8"))
            if value.get("event") != "ready":
                raise RuntimeError(str(value.get("error", "Far-end reference отклонён.")))
            if int(value.get("frame_bytes", 0)) != self.frame_bytes:
                raise RuntimeError("Far-end service вернул несовместимый PCM-формат.")
            connection.settimeout(None)
            self._connection = connection
        except Exception:
            connection.close()
            raise

    def publish(self, mono_pcm: bytes) -> None:
        converted, self._state = ratecv(
            mono_pcm,
            2,
            1,
            self.input_rate,
            self.output_rate,
            self._state,
        )
        self._pending.extend(converted)
        while len(self._pending) >= self.frame_bytes:
            frame = bytes(self._pending[: self.frame_bytes])
            del self._pending[: self.frame_bytes]
            self._connection.sendall(frame)

    def close(self) -> None:
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()


class PcmPlaybackController:
    """Interruptible PCM output that exposes the exact mono render reference."""

    def __init__(
        self,
        selector: str,
        *,
        far_host: str = "",
        far_port: int = 0,
        far_token: str = "",
    ) -> None:
        self.selector = selector.strip()
        self.far_host = far_host
        self.far_port = int(far_port)
        self.far_token = far_token
        self._lock = threading.Lock()
        self._stream = None
        self._generation = 0
        self.output_route = self.selector or "windows_default"

    def generation(self) -> int:
        with self._lock:
            return self._generation

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass

    @staticmethod
    def _stereo_frame(mono_pcm: bytes, channels: int) -> bytes:
        if channels == 1:
            return mono_pcm
        import numpy as np

        mono = np.frombuffer(mono_pcm, dtype=np.int16)
        return np.repeat(mono[:, None], channels, axis=1).tobytes()

    def play(self, path: Path, generation: int) -> bool:
        import sounddevice as sd

        with wave.open(str(path), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise RuntimeError("PCM output принимает только mono int16 WAV.")
            sample_rate = source.getframerate()
            frame_samples = sample_rate // 100
            if frame_samples * 100 != sample_rate:
                raise RuntimeError("Частота WAV не поддерживает точные 10-мс кадры.")
            candidates = ranked_output_devices(sd, self.selector)
            if not candidates:
                raise RuntimeError(
                    "Не найдено выбранное устройство вывода. Проверьте маршрут Windows."
                )
            try:
                publisher = (
                    FarReferencePublisher(
                        self.far_host or "127.0.0.1",
                        self.far_port,
                        self.far_token,
                        input_rate=sample_rate,
                    )
                    if self.far_port
                    else None
                )
            except (OSError, RuntimeError, TimeoutError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Far-end reference handshake не выполнен: {type(exc).__name__}: {exc}"
                ) from exc
            errors = []
            try:
                for index, info, host_api in candidates:
                    channels = min(2, int(info.get("max_output_channels", 0)))
                    try:
                        sd.check_output_settings(
                            device=index,
                            channels=channels,
                            dtype="int16",
                            samplerate=sample_rate,
                        )
                        stream = sd.RawOutputStream(
                            samplerate=sample_rate,
                            blocksize=frame_samples,
                            dtype="int16",
                            channels=channels,
                            device=index,
                            latency="low",
                        )
                        stream.start()
                    except (OSError, ValueError, sd.PortAudioError) as exc:
                        errors.append(f"{info.get('name', index)} / {host_api}: {exc}")
                        source.rewind()
                        continue
                    try:
                        with self._lock:
                            if generation != self._generation:
                                stream.abort()
                                return False
                            self._stream = stream
                            self.output_route = (
                                f"{info.get('name', index)} / {host_api} / {sample_rate} Hz"
                            )
                        frame_bytes = frame_samples * 2
                        while generation == self.generation():
                            mono = source.readframes(frame_samples)
                            if not mono:
                                return True
                            if len(mono) < frame_bytes:
                                mono += b"\x00" * (frame_bytes - len(mono))
                            stream.write(self._stereo_frame(mono, channels))
                            if publisher is not None:
                                # Only frames accepted by the output stream are
                                # valid render reference for echo cancellation.
                                publisher.publish(mono)
                        return False
                    finally:
                        try:
                            stream.stop()
                        except Exception:
                            pass
                        stream.close()
                        with self._lock:
                            if self._stream is stream:
                                self._stream = None
                raise RuntimeError("Не удалось открыть PCM output: " + "; ".join(errors[-6:]))
            finally:
                if publisher is not None:
                    publisher.close()
