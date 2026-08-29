from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path


PLAYBACK_BACKEND = "System.Media.SoundPlayer"
OUTPUT_ROUTE = "windows_default"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def split_tts_text(text: str, *, max_chars: int = 260) -> list[str]:
    """Split long speech without dropping words or feeding Silero huge passages."""
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    max_chars = max(80, int(max_chars))
    pieces = re.split(r"(?<=[.!?…;:])\s+", normalized)
    chunks: list[str] = []
    for piece in pieces:
        remainder = piece.strip()
        while len(remainder) > max_chars:
            boundary = remainder.rfind(" ", 0, max_chars + 1)
            if boundary < max_chars // 2:
                boundary = max_chars
            chunks.append(remainder[:boundary].strip())
            remainder = remainder[boundary:].strip()
        if remainder:
            if chunks and len(chunks[-1]) + 1 + len(remainder) <= max_chars:
                chunks[-1] = f"{chunks[-1]} {remainder}"
            else:
                chunks.append(remainder)
    return chunks


def append_worker_log(path: Path, line: str, *, max_bytes: int = 2 * 1024 * 1024) -> None:
    """Keep the standalone worker log useful without letting it grow forever."""
    encoded = line.encode("utf-8", errors="replace")
    try:
        if path.exists() and path.stat().st_size + len(encoded) > max_bytes:
            older = path.with_name(path.name + ".2")
            previous = path.with_name(path.name + ".1")
            older.unlink(missing_ok=True)
            if previous.exists():
                previous.replace(older)
            path.replace(previous)
        with path.open("ab") as log:
            log.write(encoded)
    except OSError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Локальный процесс Silero TTS")
    parser.add_argument("--model", default="v5_ru")
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--model-size", type=int, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--speaker", default="aidar")
    parser.add_argument("--sample-rate", type=int, default=48000)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="cpu")
    parser.add_argument("--min-free-vram-mb", type=int, default=2048)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--leading-silence-ms", type=int, default=120)
    parser.add_argument("--cold-leading-silence-ms", type=int, default=1000)
    return parser.parse_args()


def write_wav(
    path: Path, audio, sample_rate: int, torch, *, leading_silence_ms: int = 0
) -> None:
    values = audio.detach().cpu().clamp(-1, 1)
    values = (values * 32767).to(dtype=torch.int16).numpy()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        silence_frames = max(0, int(sample_rate * leading_silence_ms / 1000))
        output.writeframes((b"\x00\x00" * silence_frames) + values.tobytes())


class PlaybackController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._player: subprocess.Popen | None = None
        self._generation = 0

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            player = self._player
        if player is not None and player.poll() is None:
            player.terminate()

    def generation(self) -> int:
        with self._lock:
            return self._generation

    def play(self, path: Path, generation: int) -> bool:
        player_script = Path(__file__).with_name("play-wav.ps1")
        with self._lock:
            if generation != self._generation:
                return False
            process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(player_script),
                    "-Path",
                    str(path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._player = process
        try:
            returncode = process.wait()
            cancelled = generation != self.generation()
            if cancelled:
                return False
            if returncode != 0:
                detail = process.stderr.read().strip() if process.stderr is not None else ""
                raise RuntimeError(
                    f"Проигрыватель завершился с кодом {returncode}: {detail}"
                )
            return True
        finally:
            with self._lock:
                if self._player is process:
                    self._player = None


def read_commands(
    commands: queue.Queue[dict | None], controller: PlaybackController
) -> None:
    for line in sys.stdin:
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(command, dict):
            continue
        if command.get("cmd") == "stop":
            controller.stop()
            while True:
                try:
                    abandoned = commands.get_nowait()
                except queue.Empty:
                    break
                if abandoned and abandoned.get("id"):
                    print(
                        json.dumps(
                            {"event": "speech_done", "id": str(abandoned["id"]), "ok": False, "cancelled": True}
                        ),
                        flush=True,
                    )
            continue
        if command.get("cmd") == "shutdown":
            controller.stop()
            commands.put(None)
            return
        command["_generation"] = controller.generation()
        commands.put(command)
    controller.stop()
    commands.put(None)


def main() -> int:
    args = parse_args()
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.runtime_dir / "worker.log"

    try:
        import torch
        torch.set_num_threads(max(1, args.threads))
        if not args.model_path.is_file():
            raise RuntimeError(f"Локальный файл Silero не найден: {args.model_path}")
        if args.model_size <= 0 or args.model_path.stat().st_size != args.model_size:
            raise RuntimeError("Размер локального файла Silero не совпал с lock-конфигурацией.")
        expected_sha256 = args.model_sha256.strip().casefold()
        actual_sha256 = sha256_file(args.model_path)
        if len(expected_sha256) != 64 or actual_sha256 != expected_sha256:
            raise RuntimeError("SHA-256 локального файла Silero не совпал с lock-конфигурацией.")
        model = torch.package.PackageImporter(str(args.model_path)).load_pickle(
            "tts_models", "model"
        )
        active_device = "cpu"
        if args.device in {"auto", "cuda"} and torch.cuda.is_available():
            free_bytes, _total_bytes = torch.cuda.mem_get_info()
            if args.device == "cuda" or free_bytes >= args.min_free_vram_mb * 1024 * 1024:
                active_device = "cuda"
        try:
            model.to(torch.device(active_device))
        except Exception as exc:
            print(
                f"TTS CUDA placement failed, using CPU: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            active_device = "cpu"
            model.to(torch.device("cpu"))
        warmup_started_at = time.monotonic()
        try:
            model.apply_tts(
                text="Голос готов.",
                speaker=args.speaker,
                sample_rate=args.sample_rate,
            )
        except Exception as exc:
            if active_device != "cuda":
                raise
            print(
                f"TTS CUDA warmup failed, using CPU: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            model.to(torch.device("cpu"))
            torch.cuda.empty_cache()
            active_device = "cpu"
            model.apply_tts(
                text="Голос готов.",
                speaker=args.speaker,
                sample_rate=args.sample_rate,
            )
        warmup_seconds = time.monotonic() - warmup_started_at
        append_worker_log(
            log_path,
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"Silero запущен: model={args.model}, speaker={args.speaker}, "
            f"device={active_device}, playback={PLAYBACK_BACKEND}, "
            f"output={OUTPUT_ROUTE}, прогрев={warmup_seconds:.2f} с, pid={os.getpid()}\n",
        )
        print(
            json.dumps(
                {
                    "event": "ready",
                    "device": active_device,
                    "warmup_ms": round(warmup_seconds * 1000),
                    "pid": os.getpid(),
                    "playback_backend": PLAYBACK_BACKEND,
                    "output_route": OUTPUT_ROUTE,
                }
            ),
            flush=True,
        )
    except Exception as exc:  # pragma: no cover - depends on local voice install
        append_worker_log(log_path, f"Silero не запустился: {exc!r}\n")
        print(
            json.dumps({"event": "worker_error", "error": str(exc)}),
            flush=True,
        )
        return 2

    counter = 0
    commands: queue.Queue[dict | None] = queue.Queue()
    controller = PlaybackController()
    reader = threading.Thread(target=read_commands, args=(commands, controller), daemon=True)
    reader.start()
    last_playback_at = 0.0
    while True:
        command = commands.get()
        if command is None:
            break
        text = str(command.get("text", "")).strip()
        if not text:
            continue
        speaker = str(command.get("speaker", args.speaker))
        request_id = str(command.get("id", ""))
        succeeded = False
        cancelled = False
        generation = int(command.get("_generation", -1))
        utterance_started_at = time.monotonic()
        leading_silence_ms = 0
        synthesis_seconds = 0.0
        playback_seconds = 0.0
        audio_duration_ms = 0
        failure = ""
        chunk_count = 0
        suspiciously_short = False
        try:
            synthesis_started_at = time.monotonic()
            chunks = split_tts_text(text)
            chunk_count = len(chunks)
            rendered = []
            pause = torch.zeros(round(args.sample_rate * 0.11), dtype=torch.float32)
            for index, chunk in enumerate(chunks):
                if generation != controller.generation():
                    cancelled = True
                    break
                try:
                    chunk_audio = model.apply_tts(
                        text=chunk,
                        speaker=speaker,
                        sample_rate=args.sample_rate,
                    )
                except Exception as exc:
                    if active_device != "cuda":
                        raise
                    print(
                        f"TTS CUDA synthesis failed, using CPU: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    model.to(torch.device("cpu"))
                    torch.cuda.empty_cache()
                    active_device = "cpu"
                    chunk_audio = model.apply_tts(
                        text=chunk,
                        speaker=speaker,
                        sample_rate=args.sample_rate,
                    )
                if index:
                    rendered.append(pause)
                rendered.append(chunk_audio.detach().cpu().flatten())
            if cancelled or not rendered:
                audio = torch.zeros(0, dtype=torch.float32)
            else:
                audio = torch.cat(rendered)
            synthesis_seconds = time.monotonic() - synthesis_started_at
            if cancelled:
                continue
            counter += 1
            wav_path = args.runtime_dir / f"speech-{os.getpid()}-{counter}.wav"
            idle_seconds = time.monotonic() - last_playback_at
            leading_silence_ms = (
                args.cold_leading_silence_ms
                if idle_seconds >= 5.0
                else args.leading_silence_ms
            )
            write_wav(
                wav_path,
                audio,
                args.sample_rate,
                torch,
                leading_silence_ms=leading_silence_ms,
            )
            audio_duration_ms = round(
                (audio.numel() / args.sample_rate) * 1000 + leading_silence_ms
            )
            letter_count = len(re.findall(r"[A-Za-zА-Яа-яЁё]", text))
            suspiciously_short = (
                letter_count >= 80
                and audio_duration_ms - leading_silence_ms < letter_count * 25
            )
            try:
                if request_id:
                    print(
                        json.dumps(
                            {
                                "event": "speech_started",
                                "id": request_id,
                                "device": active_device,
                                "text_chars": len(text),
                                "synthesis_ms": round(synthesis_seconds * 1000),
                                "audio_duration_ms": audio_duration_ms,
                                "chunk_count": chunk_count,
                                "audio_suspiciously_short": suspiciously_short,
                                "leading_silence_ms": leading_silence_ms,
                                "playback_backend": PLAYBACK_BACKEND,
                                "output_route": OUTPUT_ROUTE,
                            }
                        ),
                        flush=True,
                    )
                playback_started_at = time.monotonic()
                succeeded = controller.play(wav_path, generation)
                playback_seconds = time.monotonic() - playback_started_at
                cancelled = generation != controller.generation()
                last_playback_at = time.monotonic()
            finally:
                wav_path.unlink(missing_ok=True)
        except Exception as exc:  # pragma: no cover - depends on audio device/model
            failure = f"{type(exc).__name__}: {exc}"
            append_worker_log(log_path, f"Ошибка озвучивания: {exc!r}\n")
        finally:
            total_seconds = time.monotonic() - utterance_started_at
            append_worker_log(
                log_path,
                f"[{datetime.now().isoformat(timespec='seconds')}] "
                f"Озвучивание id={request_id or '-'}; символов={len(text)}; "
                f"device={active_device}; синтез={synthesis_seconds:.2f} с; "
                f"воспроизведение={playback_seconds:.2f} с; "
                f"аудио={audio_duration_ms} мс; output={OUTPUT_ROUTE}; "
                f"фрагментов={chunk_count}; подозрительно_коротко={suspiciously_short}; "
                f"тишина={leading_silence_ms} мс; "
                f"время={total_seconds:.2f} с; "
                f"успех={succeeded}; отменено={cancelled}\n",
            )
            if request_id:
                print(
                    json.dumps(
                        {
                            "event": "speech_done",
                            "id": request_id,
                            "ok": succeeded,
                            "device": active_device,
                            "text_chars": len(text),
                            "synthesis_ms": round(synthesis_seconds * 1000),
                            "playback_ms": round(playback_seconds * 1000),
                            "audio_duration_ms": audio_duration_ms,
                            "chunk_count": chunk_count,
                            "audio_suspiciously_short": suspiciously_short,
                            "leading_silence_ms": leading_silence_ms,
                            "duration_ms": round(total_seconds * 1000),
                            "playback_backend": PLAYBACK_BACKEND,
                            "output_route": OUTPUT_ROUTE,
                        }
                        | ({"error": failure} if failure else {})
                        | ({"cancelled": True} if cancelled else {})
                    ),
                    flush=True,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
