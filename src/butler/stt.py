from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from butler.audio_capture import CaptureEndpoint
from butler.config import Settings
from butler.diagnostics import current_trace_fields
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception
from butler.diagnostics import milestone as diagnostic_milestone


class SpeechRecognitionError(RuntimeError):
    pass


_AUDIO_TELEMETRY_KEYS = (
    "device",
    "host_api",
    "capture_rate",
    "device_index",
    "candidate_count",
    "failed_attempt_count",
    "input_attempts",
    "threshold",
    "noise_floor",
    "average_level",
    "peak_level",
    "chunk_count",
    "voiced_chunk_count",
    "stream_status_count",
    "voice_started_ms",
    "capture_seconds",
    "endpoint_reason",
    "semantic_endpointing",
    "semantic_endpointing_error_type",
    "partial_transcript_chars",
    "recognition_seconds",
)


def _audio_telemetry(event: dict[str, object]) -> dict[str, object]:
    return {key: event[key] for key in _AUDIO_TELEMETRY_KEYS if key in event}


class SpeechRecognizer:
    def __init__(
        self,
        settings: Settings,
        capture_endpoint: CaptureEndpoint | None = None,
    ) -> None:
        self.settings = settings
        voice = settings.raw.get("voice", {})
        self.root = settings.root
        self.python = Path(str(voice.get("python", "")))
        self.worker = settings.root / "scripts" / "stt_worker.py"
        self.service_worker = settings.root / "scripts" / "stt_service.py"
        raw_model = Path(str(voice.get("wake_model", "")))
        self.model = raw_model if raw_model.is_absolute() else settings.root / raw_model
        self.sample_rate = int(voice.get("wake_sample_rate", 16000))
        self.device = str(voice.get("wake_device", ""))
        self.engine = str(voice.get("stt_engine", "auto")).casefold()
        self.whisper_model = Path(str(voice.get("stt_model", "")))
        self.whisper_device = str(voice.get("stt_device", "auto"))
        self.whisper_compute_type = str(
            voice.get("stt_compute_type", "int8_float16")
        )
        self.silence_seconds = float(voice.get("stt_silence_seconds", 0.85))
        self.no_speech_timeout_seconds = float(
            voice.get("stt_no_speech_timeout_seconds", 10.0)
        )
        self.max_command_seconds = max(
            5, int(voice.get("stt_max_command_seconds", 45))
        )
        live = settings.raw.get("live", {})
        if not isinstance(live, dict):
            live = {}
        self.semantic_endpointing = bool(
            live.get("enabled", False) and live.get("semantic_endpointing", True)
        )
        self.turn_complete_silence_seconds = float(
            live.get("turn_complete_silence_seconds", 0.45)
        )
        self.turn_ordinary_silence_seconds = float(
            live.get("turn_ordinary_silence_seconds", 0.85)
        )
        self.turn_incomplete_silence_seconds = float(
            live.get("turn_incomplete_silence_seconds", 2.2)
        )
        self._service: subprocess.Popen[str] | None = None
        self._service_info: dict[str, object] = {}
        self._events: queue.Queue[dict[str, object]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._counter = 0
        self._fallback_reason_logged = False
        self._lock = threading.RLock()
        self._request_trace_lock = threading.Lock()
        self._request_trace_fields: dict[str, dict[str, str]] = {}
        self._partial_milestones: set[str] = set()
        self.capture_endpoint = capture_endpoint
        atexit.register(self.close)

    def _read_service(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            try:
                worker_event = json.loads(line)
            except json.JSONDecodeError:
                diagnostic_event(
                    self.settings,
                    "stt",
                    "invalid_worker_event",
                    level="warning",
                    detail=line.strip(),
                )
                continue
            if isinstance(worker_event, dict):
                event_name = str(worker_event.get("event", "unknown"))
                request_id = str(worker_event.get("id", ""))
                with self._request_trace_lock:
                    trace_fields = dict(
                        self._request_trace_fields.get(request_id, {})
                    )
                    first_partial = (
                        event_name == "partial_transcript"
                        and bool(request_id)
                        and request_id not in self._partial_milestones
                    )
                    if first_partial:
                        self._partial_milestones.add(request_id)
                worker_fields = {
                    (
                        "signal_level"
                        if str(key).casefold() == "level"
                        else str(key)
                    ): value
                    for key, value in worker_event.items()
                    if key != "event"
                }
                if event_name == "partial_transcript":
                    worker_fields.pop("text", None)
                diagnostic_event(
                    self.settings,
                    "stt",
                    f"worker_{event_name}",
                    request_id=request_id,
                    **trace_fields,
                    **worker_fields,
                )
                if event_name == "voice_started":
                    diagnostic_milestone(
                        self.settings,
                        "voice_start",
                        request_id=request_id,
                        **trace_fields,
                    )
                elif event_name == "capture_completed":
                    diagnostic_milestone(
                        self.settings,
                        "voice_end",
                        request_id=request_id,
                        endpoint_reason=worker_event.get("endpoint_reason", ""),
                        **trace_fields,
                    )
                    diagnostic_milestone(
                        self.settings,
                        "turn_detected",
                        request_id=request_id,
                        endpoint_reason=worker_event.get("endpoint_reason", ""),
                        **trace_fields,
                    )
                if first_partial:
                    diagnostic_milestone(
                        self.settings,
                        "stt_partial_first",
                        request_id=request_id,
                        partial_transcript_chars=len(
                            str(worker_event.get("text", ""))
                        ),
                        **trace_fields,
                    )
                self._events.put(worker_event)
        self._events.put({"event": "worker_exit"})

    def _read_service_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            detail = line.strip()
            if detail:
                lowered = detail.casefold()
                is_error = any(
                    marker in lowered
                    for marker in ("traceback", "error:", "exception", "fatal")
                )
                diagnostic_event(
                    self.settings,
                    "stt",
                    "worker_stderr",
                    level="error" if is_error else "info",
                    detail=detail,
                )

    def _service_command(self) -> list[str]:
        command = [
            str(self.python),
            "-u",
            str(self.service_worker),
            "--model",
            str(self.whisper_model),
            "--fallback-model",
            str(self.model),
            "--sample-rate",
            str(self.sample_rate),
            "--audio-device",
            self.device,
            "--device",
            self.whisper_device,
            "--compute-type",
            self.whisper_compute_type,
            "--silence-seconds",
            str(self.silence_seconds),
            "--no-speech-timeout-seconds",
            str(self.no_speech_timeout_seconds),
        ]
        if self.semantic_endpointing:
            command.extend(
                [
                    "--semantic-endpointing",
                    "--turn-complete-silence-seconds",
                    str(self.turn_complete_silence_seconds),
                    "--turn-ordinary-silence-seconds",
                    str(self.turn_ordinary_silence_seconds),
                    "--turn-incomplete-silence-seconds",
                    str(self.turn_incomplete_silence_seconds),
                ]
            )
        if self.capture_endpoint is not None:
            command.extend(self.capture_endpoint.command_arguments())
        return command

    def _start_service(self) -> bool:
        if self.engine == "vosk" or not self.whisper_model.is_dir():
            if not self._fallback_reason_logged:
                diagnostic_event(
                    self.settings,
                    "stt",
                    "quality_service_unavailable",
                    level="warning",
                    configured_engine=self.engine,
                    whisper_model_exists=self.whisper_model.is_dir(),
                )
                self._fallback_reason_logged = True
            return False
        if self._service is not None and self._service.poll() is None:
            return True
        started = time.monotonic()
        try:
            while not self._events.empty():
                try:
                    self._events.get_nowait()
                except queue.Empty:
                    break
            worker_env = (
                self.capture_endpoint.child_environment()
                if self.capture_endpoint is not None
                else os.environ.copy()
            )
            worker_env.update(
                {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            )
            self._service = subprocess.Popen(
                self._service_command(),
                cwd=str(self.root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=worker_env,
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._reader = threading.Thread(
                target=self._read_service, args=(self._service,), daemon=True
            )
            self._reader.start()
            self._stderr_reader = threading.Thread(
                target=self._read_service_stderr,
                args=(self._service,),
                daemon=True,
            )
            self._stderr_reader.start()
            diagnostic_event(
                self.settings,
                "stt",
                "worker_started",
                worker_pid=self._service.pid,
                requested_device=self.whisper_device,
                compute_type=self.whisper_compute_type,
            )
            ready = self._events.get(timeout=180)
            if ready.get("event") != "ready":
                diagnostic_event(
                    self.settings,
                    "stt",
                    "worker_not_ready",
                    level="error",
                    worker_event=ready.get("event", ""),
                )
                self.close()
                return False
            self._service_info = dict(ready)
            diagnostic_event(
                self.settings,
                "stt",
                "ready",
                duration_ms=round((time.monotonic() - started) * 1000),
                engine=ready.get("engine", ""),
                device=ready.get("device", ""),
                compute_type=ready.get("compute_type", ""),
            )
            return True
        except (OSError, queue.Empty) as exc:
            diagnostic_exception(
                self.settings,
                "stt",
                "worker_start_failed",
                exc,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            self.close()
            return False

    def _listen_service(
        self,
        max_seconds: int,
        prompt: Callable[[], None] | None = None,
        on_partial: Callable[[str], None] | None = None,
    ) -> dict[str, object] | None:
        with self._lock:
            if not self._start_service() or self._service is None:
                return None
            if self._service.stdin is None:
                return None
            self._counter += 1
            request_id = str(self._counter)
            with self._request_trace_lock:
                self._request_trace_fields = {
                    request_id: current_trace_fields()
                }
                self._partial_milestones.discard(request_id)
            started = time.monotonic()
            diagnostic_event(
                self.settings,
                "stt",
                "listen_started",
                request_id=request_id,
                max_seconds=max_seconds,
                prompt_synchronized=prompt is not None,
            )
            try:
                self._service.stdin.write(
                    json.dumps(
                        {
                            "cmd": "prepare_listen" if prompt is not None else "listen",
                            "id": request_id,
                            "max_seconds": max_seconds,
                        }
                    )
                    + "\n"
                )
                self._service.stdin.flush()
                while True:
                    event = self._events.get(timeout=max_seconds + 120)
                    if event.get("event") == "worker_exit":
                        diagnostic_event(
                            self.settings,
                            "stt",
                            "listen_worker_exit",
                            level="error",
                            request_id=request_id,
                            duration_ms=round((time.monotonic() - started) * 1000),
                        )
                        self.close()
                        raise SpeechRecognitionError(
                            "STT-процесс завершился после начала слушания; "
                            "повторная запись без нового приглашения не запущена."
                        )
                    if str(event.get("id", "")) != request_id:
                        continue
                    if event.get("event") == "listening_ready" and prompt is not None:
                        prompt()
                        self._service.stdin.write(
                            json.dumps({"cmd": "start_listen", "id": request_id}) + "\n"
                        )
                        self._service.stdin.flush()
                        continue
                    if event.get("event") == "partial_transcript":
                        partial_text = str(event.get("text", "")).strip()
                        if partial_text and on_partial is not None:
                            try:
                                on_partial(partial_text)
                            except Exception as exc:
                                diagnostic_exception(
                                    self.settings,
                                    "stt",
                                    "partial_callback_failed",
                                    exc,
                                    request_id=request_id,
                                )
                        continue
                    if event.get("event") in {
                        "capture_started",
                        "voice_started",
                        "semantic_endpointing_unavailable",
                        "capture_completed",
                    }:
                        continue
                    break
            except (BrokenPipeError, OSError, queue.Empty) as exc:
                with self._request_trace_lock:
                    self._request_trace_fields.pop(request_id, None)
                    self._partial_milestones.discard(request_id)
                diagnostic_exception(
                    self.settings,
                    "stt",
                    "listen_service_failed",
                    exc,
                    request_id=request_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                self.close()
                raise SpeechRecognitionError(
                    "Связь со STT потеряна после начала слушания; "
                    "повторная запись без нового приглашения не запущена."
                ) from exc
            if event.get("event") != "transcript":
                with self._request_trace_lock:
                    self._request_trace_fields.pop(request_id, None)
                    self._partial_milestones.discard(request_id)
                diagnostic_event(
                    self.settings,
                    "stt",
                    "listen_failed",
                    level="error",
                    request_id=request_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    error=event.get("error", "Речь не распознана."),
                    **_audio_telemetry(event),
                )
                raise SpeechRecognitionError(str(event.get("error", "Речь не распознана.")))
            diagnostic_event(
                self.settings,
                "stt",
                "listen_completed",
                request_id=request_id,
                duration_ms=round((time.monotonic() - started) * 1000),
                transcript=event.get("text", ""),
                engine=event.get("engine", ""),
                model_device=event.get("model_device", ""),
                **_audio_telemetry(event),
            )
            diagnostic_milestone(
                self.settings,
                "stt_final",
                request_id=request_id,
                engine=event.get("engine", ""),
                model_device=event.get("model_device", ""),
            )
            with self._request_trace_lock:
                self._request_trace_fields.pop(request_id, None)
                self._partial_milestones.discard(request_id)
            return event

    def listen_after_prompt(
        self,
        prompt: Callable[[], None],
        max_seconds: int | None = None,
        *,
        on_partial: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        """Open and drain the microphone before playing the invitation to speak."""
        effective_max_seconds = max_seconds or self.max_command_seconds
        prompted = False

        def play_prompt() -> None:
            nonlocal prompted
            prompted = True
            prompt()

        service_result = self._listen_service(
            effective_max_seconds,
            play_prompt,
            on_partial,
        )
        if service_result is not None:
            return service_result
        if not prompted:
            prompt()
        return self._listen_worker(effective_max_seconds)

    def prepare(self) -> dict[str, object]:
        """Load the quality recognizer before telling the user to start speaking."""
        with self._lock:
            if self._start_service():
                return dict(self._service_info)
        return {"engine": "vosk-fallback", "device": "cpu"}

    def listen_once(
        self,
        max_seconds: int | None = None,
        *,
        on_partial: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        effective_max_seconds = max_seconds or self.max_command_seconds
        if not self.python.is_file():
            raise SpeechRecognitionError(f"Не найден голосовой Python: {self.python}")
        if not self.worker.is_file():
            raise SpeechRecognitionError(f"Не найден STT-слушатель: {self.worker}")
        if not self.model.is_dir():
            raise SpeechRecognitionError(f"Не найдена модель распознавания: {self.model}")

        service_result = self._listen_service(
            effective_max_seconds,
            on_partial=on_partial,
        )
        if service_result is not None:
            return service_result

        return self._listen_worker(effective_max_seconds)

    def _listen_worker(self, max_seconds: int) -> dict[str, object]:
        started = time.monotonic()
        diagnostic_event(
            self.settings,
            "stt",
            "fallback_listen_started",
            max_seconds=max_seconds,
        )
        try:
            worker_command = [
                str(self.python),
                "-u",
                str(self.worker),
                "--model",
                str(self.model),
                "--sample-rate",
                str(self.sample_rate),
                "--device",
                self.device,
                "--max-seconds",
                str(max_seconds),
                "--no-speech-timeout-seconds",
                str(self.no_speech_timeout_seconds),
            ]
            worker_env = os.environ.copy()
            if self.capture_endpoint is not None:
                worker_command.extend(self.capture_endpoint.command_arguments())
                worker_env = self.capture_endpoint.child_environment()
            worker_env.update(
                {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
            )
            result = subprocess.run(
                worker_command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=worker_env,
                timeout=max_seconds + 30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            diagnostic_exception(
                self.settings,
                "stt",
                "fallback_listen_failed",
                exc,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise SpeechRecognitionError(f"Не удалось запустить распознавание: {exc}") from exc
        output = result.stdout.strip().splitlines()
        if not output:
            raise SpeechRecognitionError(
                result.stderr.strip() or "Распознавание завершилось без результата."
            )
        try:
            event = json.loads(output[-1])
        except json.JSONDecodeError as exc:
            raise SpeechRecognitionError("Распознаватель вернул повреждённый результат.") from exc
        if event.get("event") != "transcript":
            diagnostic_event(
                self.settings,
                "stt",
                "fallback_listen_failed",
                level="error",
                duration_ms=round((time.monotonic() - started) * 1000),
                error=event.get("error", "Речь не распознана."),
                **_audio_telemetry(event),
            )
            raise SpeechRecognitionError(str(event.get("error", "Речь не распознана.")))
        diagnostic_event(
            self.settings,
            "stt",
            "fallback_listen_completed",
            duration_ms=round((time.monotonic() - started) * 1000),
            transcript=event.get("text", ""),
            engine=event.get("engine", ""),
            **_audio_telemetry(event),
        )
        diagnostic_milestone(
            self.settings,
            "stt_final",
            engine=event.get("engine", ""),
            model_device=event.get("model_device", ""),
            fallback=True,
        )
        return event

    def close(self) -> None:
        with self._lock:
            process = self._service
            reader = self._reader
            stderr_reader = self._stderr_reader
            self._service = None
            self._reader = None
            self._stderr_reader = None
            self._service_info = {}
        with self._request_trace_lock:
            self._request_trace_fields.clear()
            self._partial_milestones.clear()
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write('{"cmd":"shutdown"}\n')
                process.stdin.flush()
                process.stdin.close()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2)
        if stderr_reader is not None and stderr_reader is not threading.current_thread():
            stderr_reader.join(timeout=2)
        diagnostic_event(
            self.settings,
            "stt",
            "worker_stopped",
            returncode=process.poll(),
        )

    def list_devices(self) -> list[dict[str, object]]:
        worker = self.root / "scripts" / "audio_devices.py"
        if not self.python.is_file():
            raise SpeechRecognitionError(f"Не найден голосовой Python: {self.python}")
        try:
            result = subprocess.run(
                [str(self.python), "-u", str(worker)],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            event = json.loads(result.stdout.strip().splitlines()[-1])
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError) as exc:
            raise SpeechRecognitionError(f"Не удалось получить список микрофонов: {exc}") from exc
        if event.get("event") != "devices":
            raise SpeechRecognitionError(str(event.get("error", "Микрофоны не найдены.")))
        devices = event.get("devices", [])
        return devices if isinstance(devices, list) else []
