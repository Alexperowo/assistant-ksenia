from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import time
from collections import deque
from pathlib import Path

from audio_input import open_best_input_stream
from pcm_audio import ratecv, rms


_CUDA_DLL_HANDLES: list[object] = []


class AudioCaptureError(RuntimeError):
    def __init__(self, message: str, telemetry: dict[str, object]) -> None:
        super().__init__(message)
        self.telemetry = telemetry


def configure_cuda_dll_paths() -> None:
    """Expose the CUDA libraries bundled with the verified PyTorch wheel."""
    if os.name != "nt":
        return
    torch_lib = Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
    if not torch_lib.is_dir():
        return
    os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        _CUDA_DLL_HANDLES.append(os.add_dll_directory(str(torch_lib)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Постоянное локальное распознавание речи")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--fallback-model", type=Path, required=True)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--audio-device", default="")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--silence-seconds", type=float, default=0.85)
    parser.add_argument("--no-speech-timeout-seconds", type=float, default=10.0)
    return parser.parse_args()


def load_whisper(args):
    configure_cuda_dll_paths()
    from faster_whisper import WhisperModel
    import numpy as np

    attempts = []
    profiles = []
    if args.device in {"auto", "cuda"}:
        profiles.append(("cuda", args.compute_type))
    if args.device in {"auto", "cpu"}:
        profiles.append(("cpu", "int8"))
    for device, compute_type in profiles:
        try:
            model = WhisperModel(str(args.model), device=device, compute_type=compute_type)
            # CTranslate2 can construct a CUDA model even when runtime DLLs are
            # missing. Force one tiny encoder pass before announcing readiness.
            if device == "cuda":
                segments, _info = model.transcribe(
                    np.zeros(16000, dtype=np.float32),
                    language="ru",
                    beam_size=1,
                    vad_filter=False,
                    condition_on_previous_text=False,
                )
                list(segments)
            return (model, device, compute_type)
        except Exception as exc:
            attempts.append(f"{device}/{compute_type}: {exc}")
            print(
                f"STT profile failed: {device}/{compute_type}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    raise RuntimeError("; ".join(attempts) or "Whisper не загрузился.")


def _input_metadata(opened) -> dict[str, object]:
    return {
        "device": opened.device_name,
        "host_api": opened.host_api,
        "capture_rate": opened.sample_rate,
        "device_index": opened.device_index,
        "candidate_count": opened.candidate_count,
        "failed_attempt_count": len(opened.failed_attempts),
        "input_attempts": list(opened.failed_attempts),
    }


def capture_phrase(args, max_seconds: int, before_capture=None, on_event=None):
    import sounddevice as sd

    audio_queue: queue.Queue[bytes] = queue.Queue()
    stream_status_count = 0

    def callback(indata, frames, callback_time, status) -> None:
        nonlocal stream_status_count
        if status:
            stream_status_count += 1
        if len(indata):
            audio_queue.put(bytes(indata))

    opened = open_best_input_stream(
        sd, args.audio_device, callback, target_rate=args.sample_rate
    )
    try:
        if before_capture is not None:
            before_capture(opened)
        # Discard the delayed wake phrase and the assistant's own prompt that
        # accumulated while the Bluetooth input stream was being prepared.
        while True:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                break
        capture_started_at = time.monotonic()
        deadline = capture_started_at + max_seconds
        no_speech_deadline = capture_started_at + min(
            max_seconds, max(2.0, float(args.no_speech_timeout_seconds))
        )
        resample_state = None
        pre_roll: deque[bytes] = deque(maxlen=4)
        captured = bytearray()
        noise_levels: list[int] = []
        threshold = 220
        speech_started = False
        last_voice = time.monotonic()
        level_sum = 0
        chunk_count = 0
        voiced_chunk_count = 0
        peak_level = 0
        voice_started_ms: int | None = None
        endpoint_reason = "max_duration"
        if on_event is not None:
            on_event(
                "capture_started",
                **_input_metadata(opened),
                max_seconds=max_seconds,
                no_speech_timeout_seconds=float(args.no_speech_timeout_seconds),
                silence_seconds=float(args.silence_seconds),
            )
        while time.monotonic() < deadline:
            if not speech_started and time.monotonic() >= no_speech_deadline:
                endpoint_reason = "no_speech"
                break
            try:
                data = audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if opened.sample_rate != args.sample_rate:
                data, resample_state = ratecv(
                    data, 2, 1, opened.sample_rate, args.sample_rate, resample_state
                )
            level = rms(data, 2) if data else 0
            chunk_count += 1
            level_sum += level
            peak_level = max(peak_level, level)
            if not speech_started and len(noise_levels) < 4:
                noise_levels.append(level)
                threshold = max(160, int((sum(noise_levels) / len(noise_levels)) * 2.2))
            now = time.monotonic()
            pre_roll.append(data)
            if level >= threshold:
                voiced_chunk_count += 1
                if not speech_started:
                    speech_started = True
                    voice_started_ms = round((now - capture_started_at) * 1000)
                    if on_event is not None:
                        on_event(
                            "voice_started",
                            level=level,
                            threshold=threshold,
                            peak_level=peak_level,
                            voice_started_ms=voice_started_ms,
                        )
                    for buffered in pre_roll:
                        captured.extend(buffered)
                else:
                    captured.extend(data)
                last_voice = now
            elif speech_started:
                captured.extend(data)
                if now - last_voice >= args.silence_seconds:
                    endpoint_reason = "end_of_speech"
                    break
        capture_seconds = time.monotonic() - capture_started_at
        telemetry = {
            **_input_metadata(opened),
            "threshold": threshold,
            "noise_floor": round(sum(noise_levels) / len(noise_levels))
            if noise_levels
            else 0,
            "average_level": round(level_sum / chunk_count) if chunk_count else 0,
            "peak_level": peak_level,
            "chunk_count": chunk_count,
            "voiced_chunk_count": voiced_chunk_count,
            "stream_status_count": stream_status_count,
            "voice_started_ms": voice_started_ms,
            "capture_seconds": round(capture_seconds, 2),
            "endpoint_reason": endpoint_reason,
        }
        if on_event is not None:
            on_event("capture_completed", **telemetry)
        if not speech_started or not captured:
            raise AudioCaptureError(
                "Речь не обнаружена. Говорите после слова «Слушаю».", telemetry
            )
        return bytes(captured), opened, telemetry
    except Exception:
        opened.close()
        raise


def whisper_text(model, pcm: bytes, sample_rate: int) -> str:
    import numpy as np

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _info = model.transcribe(
        audio,
        language="ru",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()


def vosk_text(model_path: Path, pcm: bytes, sample_rate: int) -> str:
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    recognizer = KaldiRecognizer(Model(str(model_path)), sample_rate)
    recognizer.AcceptWaveform(pcm)
    return str(json.loads(recognizer.FinalResult()).get("text", "")).strip()


def main() -> int:
    args = parse_args()
    model, device, compute_type = load_whisper(args)
    print(
        json.dumps(
            {
                "event": "ready",
                "engine": "faster-whisper",
                "device": device,
                "compute_type": compute_type,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    for line in sys.stdin:
        try:
            command = json.loads(line)
            if command.get("cmd") == "shutdown":
                return 0
            if command.get("cmd") not in {"listen", "prepare_listen"}:
                continue
            request_id = str(command.get("id", ""))
            opened = None
            try:
                prepare_before_prompt = command.get("cmd") == "prepare_listen"

                def wait_for_start(opened_input) -> None:
                    print(
                        json.dumps(
                            {
                                "event": "listening_ready",
                                "id": request_id,
                                "device": opened_input.device_name,
                                "host_api": opened_input.host_api,
                                "capture_rate": opened_input.sample_rate,
                                "device_index": opened_input.device_index,
                                "candidate_count": opened_input.candidate_count,
                                "failed_attempt_count": len(opened_input.failed_attempts),
                                "input_attempts": list(opened_input.failed_attempts),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    while True:
                        start_line = sys.stdin.readline()
                        if not start_line:
                            raise RuntimeError("Команда начала записи не получена.")
                        start_command = json.loads(start_line)
                        if start_command.get("cmd") == "shutdown":
                            raise RuntimeError("Распознавание остановлено.")
                        if (
                            start_command.get("cmd") == "start_listen"
                            and str(start_command.get("id", "")) == request_id
                        ):
                            return

                def emit_capture_event(event_name: str, **details: object) -> None:
                    print(
                        json.dumps(
                            {"event": event_name, "id": request_id, **details},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                pcm, opened, capture_telemetry = capture_phrase(
                    args,
                    min(120, max(2, int(command.get("max_seconds", 25)))),
                    before_capture=wait_for_start if prepare_before_prompt else None,
                    on_event=emit_capture_event,
                )
                text = ""
                engine = "faster-whisper"
                whisper_error = ""
                recognition_started_at = time.monotonic()
                try:
                    text = whisper_text(model, pcm, args.sample_rate)
                except Exception as exc:
                    whisper_error = str(exc)
                if not text:
                    text = vosk_text(args.fallback_model, pcm, args.sample_rate)
                    engine = "vosk-fallback"
                if not text:
                    raise RuntimeError(whisper_error or "Речь не распознана.")
                event = {
                    "event": "transcript",
                    "id": request_id,
                    "text": text,
                    "engine": engine,
                    "model_device": device,
                    "device": opened.device_name,
                    "host_api": opened.host_api,
                    "capture_rate": opened.sample_rate,
                    "device_index": opened.device_index,
                    "candidate_count": opened.candidate_count,
                    "failed_attempt_count": len(opened.failed_attempts),
                    "input_attempts": list(opened.failed_attempts),
                    **capture_telemetry,
                    "recognition_seconds": round(
                        time.monotonic() - recognition_started_at, 2
                    ),
                }
                if engine == "vosk-fallback" and whisper_error:
                    event["fallback_reason"] = whisper_error
            except Exception as exc:
                telemetry = (
                    exc.telemetry if isinstance(exc, AudioCaptureError) else {}
                )
                event = {
                    "event": "error",
                    "id": request_id,
                    "error": str(exc),
                    **telemetry,
                }
            finally:
                if opened is not None:
                    opened.close()
            print(json.dumps(event, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"event": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"event": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
        raise SystemExit(2)
