from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from typing import Any, Callable


TOKEN_ENVIRONMENT_KEY = "KSENIA_AUDIO_CAPTURE_TOKEN"


@dataclass
class OpenedInput:
    stream: Any
    device_index: int
    device_name: str
    host_api: str
    sample_rate: int
    failed_attempts: tuple[str, ...] = ()
    candidate_count: int = 0

    def close(self) -> None:
        try:
            self.stream.stop()
        finally:
            self.stream.close()


class RemoteInputStream:
    """PortAudio-compatible reader for the authenticated local capture worker."""

    def __init__(
        self,
        connection: socket.socket,
        callback: Callable[..., None],
        frame_bytes: int,
    ) -> None:
        self._connection = connection
        self._callback = callback
        self._frame_bytes = frame_bytes
        self._stopped = threading.Event()
        self.ended = threading.Event()
        self._thread = threading.Thread(target=self._read_frames, daemon=True)
        self._thread.start()

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size and not self._stopped.is_set():
            try:
                chunk = self._connection.recv(size - len(chunks))
            except OSError:
                break
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    def _read_frames(self) -> None:
        try:
            while not self._stopped.is_set():
                data = self._read_exact(self._frame_bytes)
                if len(data) != self._frame_bytes:
                    return
                self._callback(data, len(data) // 2, None, None)
        finally:
            self.ended.set()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._connection.close()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def close(self) -> None:
        self.stop()


def _host_api_name(sd, info: dict[str, Any]) -> str:
    try:
        return str(sd.query_hostapis(int(info.get("hostapi", -1))).get("name", ""))
    except (TypeError, ValueError, sd.PortAudioError):
        return ""


def _default_input_index(sd) -> int | None:
    try:
        index = int(sd.default.device[0])
    except (AttributeError, IndexError, TypeError, ValueError, sd.PortAudioError):
        return None
    return index if index >= 0 else None


def ranked_input_devices(sd, selector: str) -> list[tuple[int, dict[str, Any], str]]:
    devices = []
    host_priority = {
        "windows wasapi": 0,
        "mme": 10,
        "windows directsound": 20,
        "windows wdm-ks": 100,
    }
    selector = selector.strip()
    default_index = _default_input_index(sd) if not selector else None
    numeric_selector: int | None = None
    try:
        numeric_selector = int(selector) if selector else None
    except ValueError:
        pass
    needle = selector.casefold()

    for index, raw_info in enumerate(sd.query_devices()):
        info = dict(raw_info)
        if int(info.get("max_input_channels", 0)) < 1:
            continue
        name = str(info.get("name", ""))
        host = _host_api_name(sd, info)
        if numeric_selector is not None and index != numeric_selector:
            continue
        if needle and numeric_selector is None and needle not in name.casefold():
            continue
        score = host_priority.get(host.casefold(), 50)
        if default_index is not None and index == int(default_index):
            score = -10
        if needle and name.casefold() == needle:
            score -= 5
        if "headset" in name.casefold() or "hands-free" in name.casefold():
            score -= 2
        devices.append((score, index, info, host))

    devices.sort(key=lambda item: (item[0], item[1]))
    return [(index, info, host) for _, index, info, host in devices]


def _automatic_input_role(name: str) -> str:
    """Classify generic Windows endpoints without depending on a device brand."""
    normalized = " ".join(str(name or "").casefold().replace("_", " ").split())
    blocked_hints = (
        "stereo mix",
        "stereo input",
        "what u hear",
        "loopback",
        "line input",
        "стерео микшер",
        "линейный вход",
        "лин. вход",
    )
    if any(hint in normalized for hint in blocked_hints):
        return "blocked"
    communication_hints = (
        "headset",
        "hands-free",
        "hands free",
        "головной телефон",
        "гарнитур",
    )
    if any(hint in normalized for hint in communication_hints):
        return "communication"
    microphone_hints = ("microphone", "mic input", "микрофон")
    if any(hint in normalized for hint in microphone_hints):
        return "microphone"
    return "unknown"


def automatic_input_devices(sd) -> list[tuple[int, dict[str, Any], str]]:
    """Choose safe speech endpoints while ignoring loopback and line sources.

    A usable Windows speech default wins. Otherwise communications endpoints are
    tried before ordinary microphone jacks. Unknown/generic recording routes are
    never selected silently.
    """
    candidates = ranked_input_devices(sd, "")
    classified = [
        (candidate, _automatic_input_role(str(candidate[1].get("name", ""))))
        for candidate in candidates
    ]
    default_index = _default_input_index(sd)
    default_role = next(
        (
            role
            for (index, _info, _host), role in classified
            if index == default_index and role in {"communication", "microphone"}
        ),
        "",
    )
    selected_role = default_role or (
        "communication"
        if any(role == "communication" for _candidate, role in classified)
        else "microphone"
    )
    return [
        candidate
        for candidate, role in classified
        if role == selected_role
    ]


def open_best_input_stream(
    sd,
    selector: str,
    callback: Callable[..., None],
    *,
    target_rate: int = 16000,
) -> OpenedInput:
    selector = selector.strip()
    candidates = (
        ranked_input_devices(sd, selector)
        if selector
        else automatic_input_devices(sd)
    )
    if not candidates:
        detail = (
            f": {selector}"
            if selector
            else ". Откройте ярлык «Ксения — список микрофонов»"
        )
        raise RuntimeError(f"Не найден подходящий речевой микрофон{detail}")

    attempts: list[str] = []
    for index, info, host in candidates:
        default_rate = max(8000, int(round(float(info.get("default_samplerate", target_rate)))))
        formats: list[tuple[int, Any]] = []
        if "wasapi" in host.casefold():
            formats.append(
                (target_rate, sd.WasapiSettings(exclusive=False, auto_convert=True))
            )
        formats.extend([(default_rate, None), (target_rate, None), (8000, None)])
        seen_rates: set[tuple[int, bool]] = set()
        for sample_rate, extra_settings in formats:
            key = (sample_rate, extra_settings is not None)
            if key in seen_rates:
                continue
            seen_rates.add(key)
            try:
                sd.check_input_settings(
                    device=index,
                    channels=1,
                    dtype="int16",
                    samplerate=sample_rate,
                    extra_settings=extra_settings,
                )
                stream = sd.RawInputStream(
                    samplerate=sample_rate,
                    blocksize=max(800, sample_rate // 4),
                    dtype="int16",
                    channels=1,
                    device=index,
                    callback=callback,
                    extra_settings=extra_settings,
                )
                stream.start()
                return OpenedInput(
                    stream=stream,
                    device_index=index,
                    device_name=str(info.get("name", index)),
                    host_api=host,
                    sample_rate=sample_rate,
                    failed_attempts=tuple(attempts),
                    candidate_count=len(candidates),
                )
            except (OSError, ValueError, sd.PortAudioError) as exc:
                attempts.append(
                    f"{info.get('name', index)} / {host} / {sample_rate} Гц: {exc}"
                )

    detail = "; ".join(attempts[-8:])
    raise RuntimeError(f"Не удалось открыть микрофон. Проверенные режимы: {detail}")


def open_remote_input_stream(
    host: str,
    port: int,
    token: str,
    callback: Callable[..., None],
    *,
    timeout: float = 5.0,
) -> OpenedInput:
    """Subscribe to PCM from the single local microphone owner."""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("Удалённый аудиозахват разрешён только через loopback.")
    if not token or len(token) < 32:
        raise RuntimeError("Отсутствует ключ локального аудиозахвата.")
    connection = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        connection.sendall(token.encode("ascii") + b"\n")
        header = bytearray()
        while len(header) <= 8192:
            chunk = connection.recv(1)
            if not chunk or chunk == b"\n":
                break
            header.extend(chunk)
        header_line = bytes(header)
        if not header_line or len(header_line) > 8192:
            raise RuntimeError("Сервис аудиозахвата не вернул корректный заголовок.")
        header = json.loads(header_line.decode("utf-8"))
        if header.get("event") != "subscribed":
            raise RuntimeError(str(header.get("error", "Подписка на микрофон отклонена.")))
        frame_bytes = int(header.get("frame_bytes", 0))
        sample_rate = int(header.get("sample_rate", 0))
        if frame_bytes <= 0 or sample_rate <= 0:
            raise RuntimeError("Сервис аудиозахвата вернул повреждённый формат PCM.")
        connection.settimeout(None)
        stream = RemoteInputStream(connection, callback, frame_bytes)
        return OpenedInput(
            stream=stream,
            device_index=int(header.get("device_index", -1)),
            device_name=str(header.get("device_name", "AudioCaptureService")),
            host_api=str(header.get("host_api", "local capture service")),
            sample_rate=sample_rate,
            failed_attempts=tuple(header.get("failed_attempts", [])),
            candidate_count=int(header.get("candidate_count", 1)),
        )
    except Exception:
        connection.close()
        raise


def open_configured_input_stream(
    sd,
    selector: str,
    callback: Callable[..., None],
    *,
    target_rate: int = 16000,
    capture_host: str = "",
    capture_port: int = 0,
    capture_token: str = "",
) -> OpenedInput:
    if capture_port:
        return open_remote_input_stream(
            capture_host or "127.0.0.1",
            capture_port,
            capture_token,
            callback,
        )
    return open_best_input_stream(
        sd,
        selector,
        callback,
        target_rate=target_rate,
    )


def input_stream_ended(opened: OpenedInput) -> bool:
    ended = getattr(opened.stream, "ended", None)
    return bool(ended is not None and ended.is_set())
