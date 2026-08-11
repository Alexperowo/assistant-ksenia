from __future__ import annotations

import argparse
import json
import queue
import sys
import time
from pathlib import Path

from audio_input import open_best_input_stream
from pcm_audio import ratecv, rms


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Локальное распознавание одной русской фразы")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--device", default="")
    parser.add_argument("--max-seconds", type=int, default=25)
    parser.add_argument("--no-speech-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--silence-seconds", type=float, default=1.3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    opened = None
    telemetry: dict[str, object] = {}
    try:
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        audio_queue: queue.Queue[bytes] = queue.Queue()
        stream_status_count = 0

        def callback(indata, frames, callback_time, status) -> None:
            nonlocal stream_status_count
            if status:
                stream_status_count += 1
            if len(indata):
                audio_queue.put(bytes(indata))

        opened = open_best_input_stream(
            sd,
            args.device,
            callback,
            target_rate=args.sample_rate,
        )
        recognizer = KaldiRecognizer(Model(str(args.model)), args.sample_rate)
        capture_started_at = time.monotonic()
        deadline = capture_started_at + args.max_seconds
        no_speech_deadline = capture_started_at + min(
            args.max_seconds, max(2.0, args.no_speech_timeout_seconds)
        )
        resample_state = None
        noise_levels: list[int] = []
        threshold = 250
        speech_started = False
        last_voice = time.monotonic()
        level_sum = 0
        chunk_count = 0
        voiced_chunk_count = 0
        peak_level = 0
        voice_started_ms: int | None = None
        endpoint_reason = "max_duration"

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
                    data,
                    2,
                    1,
                    opened.sample_rate,
                    args.sample_rate,
                    resample_state,
                )
            level = rms(data, 2) if data else 0
            chunk_count += 1
            level_sum += level
            peak_level = max(peak_level, level)
            if not speech_started and len(noise_levels) < 4:
                noise_levels.append(level)
                threshold = max(180, int((sum(noise_levels) / len(noise_levels)) * 2.5))
            recognizer.AcceptWaveform(data)
            partial = str(json.loads(recognizer.PartialResult()).get("partial", "")).strip()
            now = time.monotonic()
            if partial or level >= threshold:
                voiced_chunk_count += 1
                if not speech_started:
                    voice_started_ms = round((now - capture_started_at) * 1000)
                speech_started = True
                last_voice = now
            if speech_started and now - last_voice >= args.silence_seconds:
                endpoint_reason = "end_of_speech"
                break

        telemetry = {
            "device": opened.device_name,
            "host_api": opened.host_api,
            "capture_rate": opened.sample_rate,
            "device_index": opened.device_index,
            "candidate_count": opened.candidate_count,
            "failed_attempt_count": len(opened.failed_attempts),
            "input_attempts": list(opened.failed_attempts),
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
            "capture_seconds": round(time.monotonic() - capture_started_at, 2),
            "endpoint_reason": endpoint_reason,
        }
        result = json.loads(recognizer.FinalResult())
        text = str(result.get("text", "")).strip()
        if not text:
            raise RuntimeError("Речь не распознана. Говорите после слова «Слушаю».")
        print(
            json.dumps(
                {
                    "event": "transcript",
                    "text": text,
                    "engine": "vosk-fallback",
                    **telemetry,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    except Exception as exc:  # pragma: no cover - depends on local audio hardware
        print(
            json.dumps(
                {"event": "error", "error": str(exc), **telemetry},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 2
    finally:
        if opened is not None:
            opened.close()


if __name__ == "__main__":
    raise SystemExit(main())
