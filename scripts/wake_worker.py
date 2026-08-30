from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import time
from pathlib import Path

from audio_input import (
    TOKEN_ENVIRONMENT_KEY,
    input_stream_ended,
    open_configured_input_stream,
)
from pcm_audio import ratecv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Локальный слушатель активационной фразы")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--phrase", default="Ксения слушай")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--device", default="")
    parser.add_argument("--capture-host", default="")
    parser.add_argument("--capture-port", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=120)
    return parser.parse_args()


def normalize(text: str) -> str:
    return " ".join(re.findall(r"[а-яё]+", text.lower()))


def is_activation(text: str, phrase: str) -> bool:
    words = normalize(text).split()
    phrase_words = normalize(phrase).split()
    if phrase_words and all(word in words for word in phrase_words):
        return True
    has_name = any(word in {"ксения", "сения"} for word in words)
    has_listen = any(word.startswith("слуш") for word in words)
    return has_name and has_listen


def main() -> int:
    args = parse_args()
    opened = None
    try:
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        audio_queue: queue.Queue[bytes] = queue.Queue()

        def callback(indata, frames, callback_time, status) -> None:
            if not status:
                audio_queue.put(bytes(indata))

        opened = open_configured_input_stream(
            sd,
            args.device,
            callback,
            target_rate=args.sample_rate,
            capture_host=args.capture_host,
            capture_port=args.capture_port,
            capture_token=os.environ.get(TOKEN_ENVIRONMENT_KEY, ""),
        )
        model = Model(str(args.model))
        grammar = list(
            dict.fromkeys(
                [
                    args.phrase.lower(),
                    "ксения слушай",
                    "сения слушай",
                    "ксения слушать",
                    "сения слушать",
                    "ксения стоп",
                    "сения стоп",
                    "[unk]",
                ]
            )
        )
        recognizer = KaldiRecognizer(
            model,
            args.sample_rate,
            json.dumps(grammar, ensure_ascii=False),
        )
        print(
            json.dumps(
                {
                    "event": "listening_ready",
                    "device": opened.device_name,
                    "host_api": opened.host_api,
                    "capture_rate": opened.sample_rate,
                    "device_index": opened.device_index,
                    "candidate_count": opened.candidate_count,
                    "failed_attempt_count": len(opened.failed_attempts),
                    "input_attempts": list(opened.failed_attempts),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        deadline = time.monotonic() + args.timeout
        resample_state = None

        while time.monotonic() < deadline:
            try:
                data = audio_queue.get(timeout=0.5)
            except queue.Empty:
                if input_stream_ended(opened):
                    raise RuntimeError("Связь с сервисом микрофона потеряна.")
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
            accepted = recognizer.AcceptWaveform(data)
            results = [recognizer.PartialResult()]
            if accepted:
                results.append(recognizer.Result())
            for raw in results:
                payload = json.loads(raw)
                text = normalize(str(payload.get("partial", "") or payload.get("text", "")))
                words = text.split()
                if "стоп" in words and any(word in {"ксения", "сения"} for word in words):
                    print(
                        json.dumps(
                            {
                                "event": "stop",
                                "text": text,
                                "device": opened.device_name,
                                "host_api": opened.host_api,
                                "capture_rate": opened.sample_rate,
                                "device_index": opened.device_index,
                                "candidate_count": opened.candidate_count,
                                "failed_attempt_count": len(opened.failed_attempts),
                                "input_attempts": list(opened.failed_attempts),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    return 0
                if is_activation(text, args.phrase):
                    print(
                        json.dumps(
                            {
                                "event": "wake",
                                "text": text,
                                "device": opened.device_name,
                                "host_api": opened.host_api,
                                "capture_rate": opened.sample_rate,
                                "device_index": opened.device_index,
                                "candidate_count": opened.candidate_count,
                                "failed_attempt_count": len(opened.failed_attempts),
                                "input_attempts": list(opened.failed_attempts),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    return 0
        print(
            json.dumps(
                {
                    "event": "timeout",
                    "error": "Фраза активации не распознана до истечения времени.",
                    "device": opened.device_name,
                    "host_api": opened.host_api,
                    "capture_rate": opened.sample_rate,
                    "device_index": opened.device_index,
                    "candidate_count": opened.candidate_count,
                    "failed_attempt_count": len(opened.failed_attempts),
                    "input_attempts": list(opened.failed_attempts),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1
    except Exception as exc:  # pragma: no cover - depends on local audio hardware
        print(json.dumps({"event": "error", "error": str(exc)}, ensure_ascii=False), flush=True)
        return 2
    finally:
        if opened is not None:
            opened.close()


if __name__ == "__main__":
    raise SystemExit(main())
