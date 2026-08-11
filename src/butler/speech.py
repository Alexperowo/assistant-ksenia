from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception
from butler.speech_text import normalize_for_speech


def _speech_metadata(original: str, spoken: str) -> dict[str, object]:
    return {
        "input_chars": len(original),
        "spoken_chars": len(spoken),
        "digit_count": len(re.findall(r"\d", original)),
        "normalization_changed": original != spoken,
    }


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
        self.speaker = str(voice_config.get("speaker", "aidar"))
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
        self._worker: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._request_counter = 0
        self._pending: dict[
            str, tuple[threading.Event | None, str, dict[str, bool]]
        ] = {}
        self._sapi_processes: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        atexit.register(self.close)

    def _silero_available(self) -> bool:
        return (
            self.enabled
            and self.engine == "silero"
            and self.python.is_file()
            and self.worker_script.is_file()
        )

    def _start_worker(self) -> bool:
        if not self._silero_available():
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "silero_unavailable",
                engine=self.engine,
                python_exists=self.python.is_file(),
                worker_exists=self.worker_script.is_file(),
            )
            return False
        if self._worker is not None and self._worker.poll() is None:
            return True
        try:
            worker_env = os.environ.copy()
            worker_env["PYTHONIOENCODING"] = "utf-8"
            worker_env["PYTHONUTF8"] = "1"
            self._worker = subprocess.Popen(
                [
                    str(self.python),
                    "-u",
                    str(self.worker_script),
                    "--model",
                    self.model,
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
                ],
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
            event_level = (
                "warning"
                if bool(worker_event.get("audio_suspiciously_short", False))
                else "info"
            )
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                f"worker_{event_name}",
                level=event_level,
                **{
                    str(key): value
                    for key, value in worker_event.items()
                    if key != "event"
                },
            )
            request_id = str(worker_event.get("id", ""))
            if not request_id or event_name != "speech_done":
                continue
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is None:
                continue
            waiter, text, result = pending
            result["ok"] = bool(worker_event.get("ok", False))
            result["cancelled"] = bool(worker_event.get("cancelled", False))
            if waiter is not None:
                waiter.set()
            elif not result["ok"] and not result["cancelled"]:
                self._speak_with_sapi(text, wait=False)

        with self._lock:
            abandoned = list(self._pending.values())
            self._pending.clear()
        for waiter, text, result in abandoned:
            result["ok"] = False
            if waiter is not None:
                waiter.set()
            else:
                self._speak_with_sapi(text, wait=False)
        diagnostic_event(
            self.diagnostics_source,
            "tts",
            "worker_exited",
            level="warning" if abandoned else "info",
            returncode=worker.poll(),
            abandoned_count=len(abandoned),
        )

    def _send_silero(
        self,
        text: str,
        speaker: str | None = None,
        *,
        wait: bool = False,
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
            self._pending[request_id] = (waiter, text, result)
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
                    text=text,
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
                text=text,
            )
            return False
        return bool(result.get("ok", False))

    def _speak_with_sapi(self, text: str, *, wait: bool) -> None:
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
            with self._lock:
                self._sapi_processes.add(process)
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "sapi_started",
                worker_pid=process.pid,
                wait=wait,
                text=text,
            )
            if wait:
                returncode = process.wait(timeout=max(30.0, len(text) / 4.0))
                detail = process.stderr.read().strip() if process.stderr is not None else ""
                with self._lock:
                    self._sapi_processes.discard(process)
                diagnostic_event(
                    self.diagnostics_source,
                    "tts",
                    "sapi_completed",
                    level="info" if returncode == 0 else "error",
                    worker_pid=process.pid,
                    returncode=returncode,
                    detail=detail,
                )
            else:
                threading.Thread(
                    target=self._forget_sapi,
                    args=(process,),
                    daemon=True,
                ).start()
        except (OSError, subprocess.TimeoutExpired) as exc:
            diagnostic_exception(
                self.diagnostics_source, "tts", "sapi_failed", exc
            )

    def _forget_sapi(self, process: subprocess.Popen) -> None:
        try:
            returncode = process.wait()
            detail = process.stderr.read().strip() if process.stderr is not None else ""
            diagnostic_event(
                self.diagnostics_source,
                "tts",
                "sapi_completed",
                level="info" if returncode == 0 else "error",
                worker_pid=process.pid,
                returncode=returncode,
                detail=detail,
            )
        finally:
            with self._lock:
                self._sapi_processes.discard(process)

    def stop(self) -> None:
        """Stop current and queued speech without shutting the voice engine down."""
        with self._lock:
            worker = self._worker
            sapi_processes = list(self._sapi_processes)
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
        diagnostic_event(
            self.diagnostics_source, "tts", "fallback_to_sapi", text=spoken_text
        )
        self._speak_with_sapi(spoken_text, wait=False)

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
        diagnostic_event(
            self.diagnostics_source, "tts", "fallback_to_sapi", text=spoken_text
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
