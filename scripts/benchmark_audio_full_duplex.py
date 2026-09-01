from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import threading
import time
import wave
from array import array
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from butler.atomic_io import atomic_write_text  # noqa: E402
from butler.audio_capture import AudioCaptureService, CaptureEndpoint  # noqa: E402
from butler.config import Settings, load_settings  # noqa: E402
from butler.speech import SpeechAnnouncer, SpeechCompletion  # noqa: E402


DEFAULT_PHRASE = (
    "Это проверка полного дуплекса. Ксения говорит через наушники, "
    "а микрофон одновременно контролирует акустическое эхо."
)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def frame_rms(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(float(value) ** 2 for value in samples) / len(samples))


def analyze_pcm_frames(
    baseline_frames: Iterable[bytes],
    measurement_frames: Iterable[bytes],
) -> dict[str, float | int]:
    baseline = [frame_rms(frame) for frame in baseline_frames]
    measured = [frame_rms(frame) for frame in measurement_frames]
    noise_p95 = percentile(baseline, 0.95)
    activity_threshold = max(80.0, noise_p95 * 2.5)
    active = [value for value in measured if value >= activity_threshold]
    return {
        "baseline_frame_count": len(baseline),
        "measurement_frame_count": len(measured),
        "baseline_rms_p50": round(percentile(baseline, 0.50), 3),
        "baseline_rms_p95": round(noise_p95, 3),
        "measurement_rms_p50": round(percentile(measured, 0.50), 3),
        "measurement_rms_p95": round(percentile(measured, 0.95), 3),
        "measurement_rms_max": round(max(measured, default=0.0), 3),
        "activity_threshold_rms": round(activity_threshold, 3),
        "active_frame_count": len(active),
        "active_frame_fraction": round(len(active) / len(measured), 4)
        if measured
        else 0.0,
        "active_rms_p50": round(percentile(active, 0.50), 3),
        "active_rms_p95": round(percentile(active, 0.95), 3),
    }


def suppression_db(reference_rms: float, processed_rms: float) -> float | None:
    if reference_rms <= 0.0 or processed_rms <= 0.0:
        return None
    return round(20.0 * math.log10(reference_rms / processed_rms), 3)


class CaptureRecorder:
    def __init__(self, endpoint: CaptureEndpoint) -> None:
        self.endpoint = endpoint
        self._frames: list[tuple[float, bytes]] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._socket = socket.create_connection(
            (endpoint.host, endpoint.port), timeout=5.0
        )
        self._socket.sendall(endpoint.token.encode("ascii") + b"\n")
        header = self._read_line()
        value = json.loads(header.decode("utf-8"))
        if value.get("event") != "subscribed":
            self._socket.close()
            raise RuntimeError(str(value.get("error", "Подписка на PCM отклонена.")))
        if int(value.get("frame_bytes", 0)) != endpoint.frame_bytes:
            self._socket.close()
            raise RuntimeError("Сервис микрофона вернул несовместимый размер кадра.")
        self._socket.settimeout(None)
        self._reader = threading.Thread(target=self._read_frames, daemon=True)
        self._reader.start()

    def _read_line(self) -> bytes:
        result = bytearray()
        while len(result) <= 8192:
            chunk = self._socket.recv(1)
            if not chunk or chunk == b"\n":
                break
            result.extend(chunk)
        return bytes(result)

    def _read_exact(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size and not self._stopped.is_set():
            try:
                chunk = self._socket.recv(size - len(result))
            except OSError:
                break
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)

    def _read_frames(self) -> None:
        while not self._stopped.is_set():
            frame = self._read_exact(self.endpoint.frame_bytes)
            if len(frame) != self.endpoint.frame_bytes:
                return
            with self._lock:
                self._frames.append((time.monotonic(), frame))

    def between(self, started_at: float, ended_at: float) -> list[bytes]:
        with self._lock:
            return [
                frame
                for captured_at, frame in self._frames
                if started_at <= captured_at <= ended_at
            ]

    def all_frames(self) -> list[bytes]:
        with self._lock:
            return [frame for _captured_at, frame in self._frames]

    def close(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        self._reader.join(timeout=2.0)


def benchmark_settings(
    settings: Settings,
    *,
    audio_processing: bool,
    stream_delay_ms: int,
    output_device: str,
) -> Settings:
    raw = deepcopy(settings.raw)
    raw["voice"]["playback_backend"] = "pcm"
    raw["voice"]["output_device"] = output_device.strip()
    raw["live"]["enabled"] = True
    processing = raw["live"].setdefault("audio_processing", {})
    processing["enabled"] = audio_processing
    processing["stream_delay_ms"] = stream_delay_ms
    return replace(settings, raw=raw)


def write_recording(path: Path, sample_rate: int, frames: Iterable[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(b"".join(frames))


def run_mode(
    base_settings: Settings,
    *,
    audio_processing: bool,
    stream_delay_ms: int,
    output_device: str,
    phrase: str,
    baseline_seconds: float,
    timeout_seconds: float,
    recording_path: Path,
) -> dict[str, object]:
    settings = benchmark_settings(
        base_settings,
        audio_processing=audio_processing,
        stream_delay_ms=stream_delay_ms,
        output_device=output_device,
    )
    capture = AudioCaptureService(settings)
    speech = SpeechAnnouncer(
        settings.root,
        enabled=True,
        voice_config=settings.raw["voice"],
        diagnostics_source=settings,
    )
    recorder: CaptureRecorder | None = None
    try:
        endpoint = capture.start()
        recorder = CaptureRecorder(endpoint)
        baseline_started_at = time.monotonic()
        time.sleep(baseline_seconds)
        baseline_ended_at = time.monotonic()
        completion_event = threading.Event()
        completion: list[SpeechCompletion] = []

        def completed(value: SpeechCompletion) -> None:
            completion.append(value)
            completion_event.set()

        measurement_started_at = time.monotonic()
        if not speech.say_tracked(phrase, completed):
            raise RuntimeError("Silero отклонил тестовую фразу.")
        if not completion_event.wait(timeout_seconds):
            speech.stop()
            raise TimeoutError("Озвучивание не завершилось за отведённое время.")
        measurement_ended_at = time.monotonic()
        result = completion[0]
        if result.engine != "silero" or not result.ok or result.cancelled:
            raise RuntimeError(
                "Тест требует успешный Silero Xenia без fallback: "
                f"engine={result.engine}, ok={result.ok}, cancelled={result.cancelled}."
            )
        time.sleep(0.25)
        baseline_frames = recorder.between(baseline_started_at, baseline_ended_at)
        measurement_frames = recorder.between(
            measurement_started_at, measurement_ended_at
        )
        write_recording(recording_path, endpoint.sample_rate, recorder.all_frames())
        return {
            "audio_processing": audio_processing,
            "stream_delay_ms": stream_delay_ms,
            "device": endpoint.device_name,
            "host_api": endpoint.host_api,
            "sample_rate": endpoint.sample_rate,
            "duration_ms": round(
                (measurement_ended_at - measurement_started_at) * 1000
            ),
            "speech": asdict(result),
            "recording": str(recording_path),
            "metrics": analyze_pcm_frames(baseline_frames, measurement_frames),
        }
    finally:
        if recorder is not None:
            recorder.close()
        speech.close()
        capture.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Физический A/B-тест PCM/AEC на текущих устройствах Windows."
    )
    parser.add_argument("--phrase", default=DEFAULT_PHRASE)
    parser.add_argument("--output-device", default="")
    parser.add_argument("--stream-delay-ms", type=int, default=0)
    parser.add_argument("--baseline-seconds", type=float, default=1.5)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.phrase.strip():
        raise ValueError("Тестовая фраза не должна быть пустой.")
    if not 0 <= args.stream_delay_ms <= 1_000:
        raise ValueError("stream-delay-ms должен быть от 0 до 1000.")
    if not 0.5 <= args.baseline_seconds <= 10.0:
        raise ValueError("baseline-seconds должен быть от 0.5 до 10.")
    if not 10.0 <= args.timeout_seconds <= 600.0:
        raise ValueError("timeout-seconds должен быть от 10 до 600.")

    settings = load_settings(ROOT)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output or (
        settings.runtime_dir / "audio-full-duplex" / timestamp / "summary.json"
    )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for enabled, label in ((False, "aec-off"), (True, "aec-on")):
        print(f"Запуск {label}...", flush=True)
        results.append(
            run_mode(
                settings,
                audio_processing=enabled,
                stream_delay_ms=args.stream_delay_ms,
                output_device=args.output_device,
                phrase=args.phrase,
                baseline_seconds=args.baseline_seconds,
                timeout_seconds=args.timeout_seconds,
                recording_path=output.parent / f"{label}.wav",
            )
        )

    off_rms = float(results[0]["metrics"]["measurement_rms_p95"])
    on_rms = float(results[1]["metrics"]["measurement_rms_p95"])
    summary = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "phrase": args.phrase,
        "output_device_selector": args.output_device,
        "results": results,
        "comparison": {
            "p95_suppression_db": suppression_db(off_rms, on_rms),
            "interpretation": (
                "exploratory_physical_measurement; do_not_enable_aec_by_default_"
                "without_repeatable_self_echo_evidence"
            ),
        },
    }
    atomic_write_text(output, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Отчёт: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
