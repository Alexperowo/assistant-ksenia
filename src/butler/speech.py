from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from butler.diagnostics import current_trace_fields
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception
from butler.diagnostics import milestone as diagnostic_milestone
from butler.diagnostics import trace_scope
from butler.speech_text import normalize_for_speech


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _speech_metadata(original: str, spoken: str) -> dict[str, object]:
    return {
        "input_chars": len(original),
        "spoken_chars": len(spoken),
        "digit_count": len(re.findall(r"\d", original)),
        "normalization_changed": original != spoken,
    }


@dataclass(frozen=True)
class SpeechCompletion:
    """Final playback result delivered exactly once for tracked speech."""

    request_id: str
    original_text: str
    spoken_text: str
    ok: bool
    cancelled: bool
    engine: str


SpeechCompletionCallback = Callable[[SpeechCompletion], None]


@dataclass
class _PendingSpeech:
    waiter: threading.Event | None
    original_text: str
    spoken_text: str
    result: dict[str, bool]
    on_complete: SpeechCompletionCallback | None = None
    trace_fields: dict[str, str] = field(default_factory=dict)


@dataclass
class _SapiSpeech:
    request_id: str
    original_text: str
    spoken_text: str
    on_complete: SpeechCompletionCallback | None = None
    cancelled: bool = False
    trace_fields: dict[str, str] = field(default_factory=dict)


class SpeechAnnouncer:
    def __init__(
        self,
        root: Path,
        enabled: bool = True,
        voice_config: dict[str, object] | None = None,
        diagnostics_source: object | None = None,
    ) -> None:
        self.enabled = enabled
        self.root = root
        self.diagnostics_source = diagnostics_source or (root / "runtime")
        self.runtime_dir = Path(
            getattr(self.diagnostics_source, "runtime_dir", root / "runtime")
        )
        self.script = root / "scripts" / "speak.ps1"
        voice_config = voice_config or {}
        self.engine = str(voice_config.get("engine", "sapi")).lower()
        self.python = Path(str(voice_config.get("python", "")))
        self.worker_script = root / "scripts" / "voice_worker.py"
        self.model = str(voice_config.get("model", "v5_ru"))
        raw_tts_model_path = str(voice_config.get("tts_model_path", "")).strip()
        if raw_tts_model_path:
            configured_tts_model = Path(raw_tts_model_path)
            self.tts_model_path = (
                configured_tts_model
                if configured_tts_model.is_absolute()
                else root / configured_tts_model
            )
        elif str(voice_config.get("python", "")).strip():
            self.tts_model_path = (
                self.python.parent.parent
                / "Lib"
                / "site-packages"
                / "silero"
                / "model"
                / f"{self.model}.pt"
            )
        else:
            self.tts_model_path = root / ".missing-silero-model"
        self.tts_model_expected_size = int(
            voice_config.get("tts_model_expected_size_bytes", 0) or 0
        )
        self.tts_model_sha256 = str(
            voice_config.get("tts_model_sha256", "")
        ).strip().casefold()
        self._tts_model_signature: tuple[int, int] | None = None
        self.speaker = str(voice_config.get("speaker", "xenia"))
        self.sample_rate = int(voice_config.get("sample_rate", 48000))
        self.threads = int(voice_config.get("threads", 4))
        self.device = str(voice_config.get("tts_device", "cpu")).casefold()
        self.min_free_vram_mb = int(
            voice_config.get("tts_min_free_vram_mb", 2048)
        )
        self.leading_silence_ms = int(voice_config.get("leading_silence_ms", 120))
        self.cold_leading_silence_ms = int(
            voice_config.get("cold_leading_silence_ms", 1000)
        )
        self.playback_backend = str(
            voice_config.get("playback_backend", "system")
        ).strip().casefold()
        self.output_device = str(voice_config.get("output_device", "")).strip()
        self._capture_endpoint = None
        self.startup_timeout_seconds = max(
            5.0, float(voice_config.get("tts_startup_timeout_seconds", 120))
        )
        self._worker: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._request_counter = 0
        self._pending: dict[str, _PendingSpeech] = {}
        self._sapi_processes: dict[subprocess.Popen, _SapiSpeech] = {}
        self._lock = threading.Lock()
        self._worker_ready = threading.Event()
        self._worker_start_error = ""
        atexit.register(self.close)

    def configure_capture_endpoint(self, endpoint: object | None) -> None:
        """Attach far-reference routing before the persistent TTS worker starts."""
        with self._lock:
            if self._worker is not None and self._worker.poll() is None:
                raise RuntimeError(
                    "Нельзя менять audio endpoint после запуска TTS worker."
                )
            self._capture_endpoint = endpoint

    def _silero_available(self) -> bool:
        if not (
            self.enabled
            and self.engine == "silero"
            and self.python.is_file()
            and self.worker_script.is_file()
            and self.tts_model_path.is_file()
            and self.tts_model_expected_size > 0
            and len(self.tts_model_sha256) == 64
        ):
            return False
        stat = self.tts_model_path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        if stat.st_size != self.tts_model_expected_size:
            return False
        if self._tts_model_signature == signature:
            return True
        actual = _sha256_file(self.tts_model_path)
        if actual != self.tts_model_sha256:
            return False
        self._tts_model_signature = signature
        return True

    def _start_worker(self) -> bool:
        if self._worker is not None and self._worker.poll() is None:
            return True
        if not self._silero_available():
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "silero_unavailable",
                engine=self.engine,
                python_exists=self.python.is_file(),
                worker_exists=self.worker_script.is_file(),
                model_exists=self.tts_model_path.is_file(),
                model_size=self.tts_model_path.stat().st_size
                if self.tts_model_path.is_file()
                else 0,
            )
            return False
        self._worker_ready.clear()
        self._worker_start_error = ""
        try:
            worker_env = os.environ.copy()
            worker_env["PYTHONIOENCODING"] = "utf-8"
            worker_env["PYTHONUTF8"] = "1"
            command = [
                str(self.python),
                "-u",
                str(self.worker_script),
                "--model",
                self.model,
                "--model-path",
                str(self.tts_model_path),
                "--model-size",
                str(self.tts_model_expected_size),
                "--model-sha256",
                self.tts_model_sha256,
                "--speaker",
                self.speaker,
                "--sample-rate",
                str(self.sample_rate),
                "--threads",
                str(self.threads),
                "--device",
                self.device,
                "--min-free-vram-mb",
                str(self.min_free_vram_mb),
                "--runtime-dir",
                str(self.runtime_dir / "voice"),
                "--leading-silence-ms",
                str(self.leading_silence_ms),
                "--cold-leading-silence-ms",
                str(self.cold_leading_silence_ms),
                "--playback-backend",
                self.playback_backend,
                "--output-device",
                self.output_device,
            ]
            endpoint = self._capture_endpoint
            if self.playback_backend == "pcm" and endpoint is not None:
                render_port = int(getattr(endpoint, "render_port", 0))
                if render_port > 0:
                    command.extend(
                        [
                            "--far-host",
                            str(getattr(endpoint, "host", "127.0.0.1")),
                            "--far-port",
                            str(render_port),
                        ]
                    )
                    child_environment = getattr(endpoint, "child_environment", None)
                    if callable(child_environment):
                        worker_env = child_environment()
                        worker_env.update(
                            {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
                        )
            self._worker = subprocess.Popen(
                command,
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
                target=self._read_worker_events,
                args=(self._worker,),
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader = threading.Thread(
                target=self._read_worker_stderr,
                args=(self._worker,),
                daemon=True,
            )
            self._stderr_reader.start()
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "worker_started",
                worker_pid=self._worker.pid,
                model=self.model,
                speaker=self.speaker,
                requested_device=self.device,
            )
            if not self._worker_ready.wait(timeout=self.startup_timeout_seconds):
                diagnostic_event(
                    self.diagnostics_source,
                    "tts",
                    "worker_ready_timeout",
                    level="error",
                    worker_pid=self._worker.pid,
                    timeout_seconds=self.startup_timeout_seconds,
                )
                if self._worker.poll() is None:
                    self._worker.terminate()
                self._worker = None
                return False
            if self._worker_start_error or self._worker.poll() is not None:
                diagnostic_event(
                    self.diagnostics_source,
                    "tts",
                    "worker_not_ready",
                    level="error",
                    error=self._worker_start_error or "worker exited",
                )
                self._worker = None
                return False
            return True
        except OSError as exc:
            diagnostic_exception(
                self.diagnostics_source, "tts", "worker_start_failed", exc
            )
            self._worker = None
            return False

    def _read_worker_stderr(self, worker: subprocess.Popen[str]) -> None:
        if worker.stderr is None:
            return
        for line in worker.stderr:
            detail = line.strip()
            if detail:
                lowered = detail.casefold()
                is_error = any(
                    marker in lowered
                    for marker in ("traceback", "error:", "exception", "fatal")
                )
                diagnostic_event(
                    self.diagnostics_source,
                    "tts",
                    "worker_stderr",
                    level="error" if is_error else "info",
                    detail=detail,
                )

    def _read_worker_events(self, worker: subprocess.Popen[str]) -> None:
        if worker.stdout is None:
            return
        for line in worker.stdout:
            try:
                worker_event = json.loads(line)
            except json.JSONDecodeError:
                diagnostic_event(
                    self.diagnostics_source,
                    "tts",
                    "invalid_worker_event",
                    level="warning",
                    detail=line.strip(),
                )
                continue
            event_name = str(worker_event.get("event", "unknown"))
            if event_name == "ready":
                self._worker_ready.set()
            elif event_name == "worker_error":
                self._worker_start_error = str(
                    worker_event.get("error", "Silero worker failed during startup.")
                )
                self._worker_ready.set()
            event_level = (
                "warning"
                if bool(worker_event.get("audio_suspiciously_short", False))
                else "info"
            )
            request_id = str(worker_event.get("id", ""))
            with self._lock:
                traced_pending = self._pending.get(request_id)
            trace_fields = (
                dict(traced_pending.trace_fields or {})
                if traced_pending is not None
                else {}
            )
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                f"worker_{event_name}",
                level=event_level,
                request_id=request_id,
                **trace_fields,
                **{
                    str(key): value
                    for key, value in worker_event.items()
                    if key != "event"
                },
            )
            if event_name == "speech_started" and request_id:
                diagnostic_milestone(
                    self.diagnostics_source,
                    "tts_first_chunk_ready",
                    request_id=request_id,
                    synthesis_ms=worker_event.get("synthesis_ms", 0),
                    **trace_fields,
                )
                diagnostic_milestone(
                    self.diagnostics_source,
                    "audio_first_played",
                    request_id=request_id,
                    output_route=worker_event.get("output_route", ""),
                    **trace_fields,
                )
            if not request_id or event_name != "speech_done":
                continue
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is None:
                continue
            completion = SpeechCompletion(
                request_id=request_id,
                original_text=pending.original_text,
                spoken_text=pending.spoken_text,
                ok=bool(worker_event.get("ok", False)),
                cancelled=bool(worker_event.get("cancelled", False)),
                engine="silero",
            )
            diagnostic_milestone(
                self.diagnostics_source,
                "audio_finished",
                request_id=request_id,
                ok=completion.ok,
                cancelled=completion.cancelled,
                **trace_fields,
            )
            if completion.cancelled:
                diagnostic_milestone(
                    self.diagnostics_source,
                    "audio_actually_stopped",
                    request_id=request_id,
                    **trace_fields,
                )
            self._complete_pending(pending, completion)

        self._worker_ready.set()
        with self._lock:
            abandoned = list(self._pending.values())
            self._pending.clear()
        for pending in abandoned:
            self._complete_pending(
                pending,
                SpeechCompletion(
                    request_id="",
                    original_text=pending.original_text,
                    spoken_text=pending.spoken_text,
                    ok=False,
                    cancelled=False,
                    engine="silero",
                ),
            )
        diagnostic_event(
            self.diagnostics_source,
            "tts",
            "worker_exited",
            level="warning" if abandoned else "info",
            returncode=worker.poll(),
            abandoned_count=len(abandoned),
        )

    def _complete_pending(
        self, pending: _PendingSpeech, completion: SpeechCompletion
    ) -> None:
        pending.result["ok"] = completion.ok
        pending.result["cancelled"] = completion.cancelled
        if pending.waiter is not None:
            pending.waiter.set()
        if pending.on_complete is None:
            return
        try:
            with trace_scope(**(pending.trace_fields or {})):
                pending.on_complete(completion)
        except Exception as exc:
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "completion_callback_failed",
                level="error",
                error_type=type(exc).__name__,
            )

    def _send_silero(
        self,
        text: str,
        speaker: str | None = None,
        *,
        wait: bool = False,
        original_text: str | None = None,
        on_complete: SpeechCompletionCallback | None = None,
    ) -> bool:
        waiter = threading.Event() if wait else None
        result: dict[str, bool] = {}
        started = time.monotonic()
        with self._lock:
            if not self._start_worker() or self._worker is None:
                return False
            if self._worker.stdin is None:
                return False
            self._request_counter += 1
            request_id = str(self._request_counter)
            self._pending[request_id] = _PendingSpeech(
                waiter=waiter,
                original_text=original_text if original_text is not None else text,
                spoken_text=text,
                result=result,
                on_complete=on_complete,
                trace_fields=current_trace_fields(),
            )
            try:
                self._worker.stdin.write(
                    json.dumps(
                        {
                            "text": text,
                            "speaker": speaker or self.speaker,
                            "id": request_id,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                self._worker.stdin.flush()
                diagnostic_event(
                    self.diagnostics_source,
                    "tts",
                    "request_queued",
                    request_id=request_id,
                    wait=wait,
                    speaker=speaker or self.speaker,
                    spoken_chars=len(text),
                )
            except (BrokenPipeError, OSError):
                if request_id:
                    self._pending.pop(request_id, None)
                self._worker = None
                diagnostic_event(
                    self.diagnostics_source,
                    "tts",
                    "request_write_failed",
                    level="error",
                    request_id=request_id,
                )
                return False
        if waiter is None:
            return True
        if not waiter.wait(timeout=max(30.0, len(text) / 4.0)):
            with self._lock:
                self._pending.pop(request_id, None)
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "request_timeout",
                level="error",
                request_id=request_id,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return False
        return bool(result.get("ok", False))

    def _speak_with_sapi(
        self,
        text: str,
        *,
        wait: bool,
        original_text: str | None = None,
        on_complete: SpeechCompletionCallback | None = None,
    ) -> bool:
        try:
            process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(self.script),
                ],
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                errors="replace",
            )
            if process.stdin is not None:
                process.stdin.write(text)
                process.stdin.close()
            sapi_speech = _SapiSpeech(
                request_id=f"sapi-{process.pid}",
                original_text=original_text if original_text is not None else text,
                spoken_text=text,
                on_complete=on_complete,
                trace_fields=current_trace_fields(),
            )
            with self._lock:
                self._sapi_processes[process] = sapi_speech
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "sapi_started",
                worker_pid=process.pid,
                wait=wait,
                spoken_chars=len(text),
            )
            if wait:
                returncode = process.wait(timeout=max(30.0, len(text) / 4.0))
                detail = process.stderr.read().strip() if process.stderr is not None else ""
                with self._lock:
                    completed = self._sapi_processes.pop(process, sapi_speech)
                diagnostic_event(
                    self.diagnostics_source,
                    "tts",
                    "sapi_completed",
                    level="info" if returncode == 0 else "error",
                    worker_pid=process.pid,
                    returncode=returncode,
                    detail=detail,
                    **(completed.trace_fields or {}),
                )
                diagnostic_milestone(
                    self.diagnostics_source,
                    "audio_finished",
                    request_id=completed.request_id,
                    ok=returncode == 0,
                    cancelled=completed.cancelled,
                    **(completed.trace_fields or {}),
                )
                self._complete_sapi(completed, returncode == 0)
                return returncode == 0
            else:
                threading.Thread(
                    target=self._forget_sapi,
                    args=(process,),
                    daemon=True,
                ).start()
            return True
        except subprocess.TimeoutExpired as exc:
            try:
                process.terminate()
            except OSError:
                pass
            with self._lock:
                timed_out = self._sapi_processes.pop(process, None)
            if timed_out is not None:
                self._complete_sapi(timed_out, False)
            diagnostic_exception(
                self.diagnostics_source, "tts", "sapi_failed", exc
            )
            return False
        except OSError as exc:
            diagnostic_exception(
                self.diagnostics_source, "tts", "sapi_failed", exc
            )
            return False

    def _complete_sapi(self, speech: _SapiSpeech, ok: bool) -> None:
        if speech.on_complete is None:
            return
        pending = _PendingSpeech(
            waiter=None,
            original_text=speech.original_text,
            spoken_text=speech.spoken_text,
            result={},
            on_complete=speech.on_complete,
            trace_fields=dict(speech.trace_fields or {}),
        )
        self._complete_pending(
            pending,
            SpeechCompletion(
                request_id=speech.request_id,
                original_text=speech.original_text,
                spoken_text=speech.spoken_text,
                ok=ok and not speech.cancelled,
                cancelled=speech.cancelled,
                engine="sapi",
            ),
        )

    def _forget_sapi(self, process: subprocess.Popen) -> None:
        completed: _SapiSpeech | None = None
        returncode = -1
        try:
            returncode = process.wait()
            detail = process.stderr.read().strip() if process.stderr is not None else ""
            with self._lock:
                tracked = self._sapi_processes.get(process)
            trace_fields = dict(tracked.trace_fields) if tracked is not None else {}
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "sapi_completed",
                level="info" if returncode == 0 else "error",
                worker_pid=process.pid,
                returncode=returncode,
                detail=detail,
                **trace_fields,
            )
        finally:
            with self._lock:
                completed = self._sapi_processes.pop(process, None)
            if completed is not None:
                diagnostic_milestone(
                    self.diagnostics_source,
                    "audio_finished",
                    request_id=completed.request_id,
                    ok=returncode == 0,
                    cancelled=completed.cancelled,
                    **(completed.trace_fields or {}),
                )
                if completed.cancelled:
                    diagnostic_milestone(
                        self.diagnostics_source,
                        "audio_actually_stopped",
                        request_id=completed.request_id,
                        **(completed.trace_fields or {}),
                    )
                self._complete_sapi(completed, returncode == 0)

    def stop(self) -> None:
        """Stop current and queued speech without shutting the voice engine down."""
        with self._lock:
            worker = self._worker
            sapi_processes = list(self._sapi_processes)
            for pending in self._sapi_processes.values():
                pending.cancelled = True
            if worker is not None and worker.stdin is not None:
                try:
                    worker.stdin.write('{"cmd":"stop"}\n')
                    worker.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._worker = None
        for process in sapi_processes:
            if process.poll() is None:
                process.terminate()
        if worker is not None or sapi_processes:
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "playback_stopped",
                sapi_process_count=len(sapi_processes),
                silero_running=worker is not None and worker.poll() is None,
            )

    def say(self, text: str) -> None:
        if not self.enabled or not text.strip() or not self.script.exists():
            return
        spoken_text = normalize_for_speech(text)
        diagnostic_event(
            self.diagnostics_source,
            "tts",
            "request_prepared",
            engine=self.engine,
            speaker=self.speaker,
            wait=False,
            **_speech_metadata(text, spoken_text),
        )
        if self._send_silero(spoken_text):
            return
        if self.playback_backend == "pcm":
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "pcm_request_failed",
                level="error",
                spoken_chars=len(spoken_text),
            )
            return
        diagnostic_event(
            self.diagnostics_source,
            "tts",
            "fallback_to_sapi",
            spoken_chars=len(spoken_text),
        )
        self._speak_with_sapi(spoken_text, wait=False)

    def say_tracked(
        self, text: str, on_complete: SpeechCompletionCallback
    ) -> bool:
        """Queue speech and report whether the whole phrase reached playback end."""
        if not self.enabled or not text.strip() or not self.script.exists():
            return False
        spoken_text = normalize_for_speech(text)
        diagnostic_event(
            self.diagnostics_source,
            "tts",
            "request_prepared",
            engine=self.engine,
            speaker=self.speaker,
            wait=False,
            tracked=True,
            **_speech_metadata(text, spoken_text),
        )
        if self._send_silero(
            spoken_text,
            original_text=text,
            on_complete=on_complete,
        ):
            return True
        diagnostic_event(
            self.diagnostics_source,
            "tts",
            "tracked_request_rejected",
            level="error",
            spoken_chars=len(spoken_text),
            tracked=True,
        )
        return False

    def live_available(self) -> bool:
        """Return whether the ordered persistent Silero queue can support Live."""
        if not self._silero_available():
            return False
        if self.playback_backend != "pcm":
            return True
        endpoint = self._capture_endpoint
        return endpoint is not None and int(getattr(endpoint, "render_port", 0)) > 0

    def say_and_wait(self, text: str) -> None:
        if not self.enabled or not text.strip() or not self.script.exists():
            return
        spoken_text = normalize_for_speech(text)
        diagnostic_event(
            self.diagnostics_source,
            "tts",
            "request_prepared",
            engine=self.engine,
            speaker=self.speaker,
            wait=True,
            **_speech_metadata(text, spoken_text),
        )
        if self._send_silero(spoken_text, wait=True):
            return
        if self.playback_backend == "pcm":
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "pcm_request_failed",
                level="error",
                spoken_chars=len(spoken_text),
            )
            return
        diagnostic_event(
            self.diagnostics_source,
            "tts",
            "fallback_to_sapi",
            spoken_chars=len(spoken_text),
        )
        self._speak_with_sapi(spoken_text, wait=True)

    def test_voices(self) -> None:
        samples = (
            ("aidar", "Это голос Айдар. Я локальный дворецкий Александра."),
            ("xenia", "Это голос Ксения. Я локальный дворецкий Александра."),
            ("eugene", "Это голос Евгений. Я локальный дворецкий Александра."),
        )
        for speaker, text in samples:
            if not self._send_silero(text, speaker, wait=True):
                if self.playback_backend == "pcm":
                    diagnostic_event(
                        self.diagnostics_source,
                        "tts",
                        "pcm_request_failed",
                        level="error",
                        spoken_chars=len(text),
                    )
                    continue
                self._speak_with_sapi(text, wait=True)

    def close(self) -> None:
        self.stop()
        with self._lock:
            worker = self._worker
            reader = self._reader
            stderr_reader = self._stderr_reader
            self._worker = None
            self._reader = None
            self._stderr_reader = None
        if worker is None:
            return
        try:
            if worker.stdin is not None:
                worker.stdin.close()
            worker.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            worker.terminate()
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=3)
        if stderr_reader is not None and stderr_reader is not threading.current_thread():
            stderr_reader.join(timeout=3)
