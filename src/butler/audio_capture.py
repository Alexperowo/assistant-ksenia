from __future__ import annotations

import atexit
import json
import os
import queue
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from butler.config import Settings
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception


TOKEN_ENVIRONMENT_KEY = "KSENIA_AUDIO_CAPTURE_TOKEN"


class AudioCaptureServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptureEndpoint:
    host: str
    port: int
    token: str = field(repr=False)
    sample_rate: int
    frame_bytes: int
    device_name: str
    host_api: str

    def child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment[TOKEN_ENVIRONMENT_KEY] = self.token
        return environment

    def command_arguments(self) -> list[str]:
        return ["--capture-host", self.host, "--capture-port", str(self.port)]


class AudioCaptureService:
    """Own the physical microphone and publish authenticated 10 ms PCM frames."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        voice = settings.raw.get("voice", {})
        self.python = Path(str(voice.get("python", "")))
        self.worker = settings.root / "scripts" / "audio_capture_service.py"
        self.device = str(voice.get("wake_device", ""))
        self.sample_rate = int(voice.get("wake_sample_rate", 16000))
        self._process: subprocess.Popen[str] | None = None
        self._endpoint: CaptureEndpoint | None = None
        self._stderr_lines: queue.Queue[str] = queue.Queue(maxsize=50)
        self._stderr_reader: threading.Thread | None = None
        self._lock = threading.RLock()
        atexit.register(self.close)

    @property
    def endpoint(self) -> CaptureEndpoint | None:
        return self._endpoint

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            detail = line.strip()
            if not detail:
                continue
            try:
                self._stderr_lines.put_nowait(detail)
            except queue.Full:
                try:
                    self._stderr_lines.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._stderr_lines.put_nowait(detail)
                except queue.Full:
                    pass

    def start(self, timeout: float = 15.0) -> CaptureEndpoint:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                if self._endpoint is None:
                    raise AudioCaptureServiceError(
                        "Сервис микрофона запущен без готового endpoint."
                    )
                return self._endpoint
            if not self.python.is_file():
                raise AudioCaptureServiceError(
                    f"Не найден голосовой Python: {self.python}"
                )
            if not self.worker.is_file():
                raise AudioCaptureServiceError(
                    f"Не найден AudioCaptureService: {self.worker}"
                )
            token = secrets.token_hex(32)
            environment = os.environ.copy()
            environment[TOKEN_ENVIRONMENT_KEY] = token
            command = [
                str(self.python),
                "-u",
                str(self.worker),
                "--device",
                self.device,
                "--sample-rate",
                str(self.sample_rate),
                "--frame-ms",
                "10",
            ]
            started = time.monotonic()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.settings.root),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                raise AudioCaptureServiceError(
                    f"Не удалось запустить сервис микрофона: {exc}"
                ) from exc
            self._process = process
            self._stderr_reader = threading.Thread(
                target=self._read_stderr,
                args=(process,),
                daemon=True,
            )
            self._stderr_reader.start()
            try:
                if process.stdout is None:
                    raise AudioCaptureServiceError(
                        "Сервис микрофона не вернул канал готовности."
                    )
                ready_lines: queue.Queue[str | None] = queue.Queue()

                def read_ready() -> None:
                    ready_lines.put(process.stdout.readline() or None)

                threading.Thread(target=read_ready, daemon=True).start()
                try:
                    ready_line = ready_lines.get(timeout=timeout)
                except queue.Empty as exc:
                    raise AudioCaptureServiceError(
                        "Сервис микрофона не запустился вовремя."
                    ) from exc
                if not ready_line:
                    detail = self._latest_error()
                    raise AudioCaptureServiceError(
                        detail or "Сервис микрофона завершился до готовности."
                    )
                ready = json.loads(ready_line)
                if ready.get("event") != "ready":
                    raise AudioCaptureServiceError(
                        str(ready.get("error", "Сервис микрофона не готов."))
                    )
                endpoint = CaptureEndpoint(
                    host=str(ready.get("host", "")),
                    port=int(ready.get("port", 0)),
                    token=token,
                    sample_rate=int(ready.get("sample_rate", 0)),
                    frame_bytes=int(ready.get("frame_bytes", 0)),
                    device_name=str(ready.get("device_name", "")),
                    host_api=str(ready.get("host_api", "")),
                )
                if (
                    endpoint.host != "127.0.0.1"
                    or endpoint.port <= 0
                    or endpoint.sample_rate <= 0
                    or endpoint.frame_bytes <= 0
                ):
                    raise AudioCaptureServiceError(
                        "Сервис микрофона вернул повреждённый endpoint."
                    )
                self._endpoint = endpoint
                diagnostic_event(
                    self.settings,
                    "audio_capture",
                    "ready",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    worker_pid=process.pid,
                    device=endpoint.device_name,
                    host_api=endpoint.host_api,
                    sample_rate=endpoint.sample_rate,
                    frame_bytes=endpoint.frame_bytes,
                )
                return endpoint
            except (AudioCaptureServiceError, json.JSONDecodeError, TypeError, ValueError) as exc:
                diagnostic_exception(
                    self.settings,
                    "audio_capture",
                    "startup_failed",
                    exc,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                self.close()
                if isinstance(exc, AudioCaptureServiceError):
                    raise
                raise AudioCaptureServiceError(
                    "Сервис микрофона вернул повреждённое событие готовности."
                ) from exc

    def _latest_error(self) -> str:
        lines = []
        while True:
            try:
                lines.append(self._stderr_lines.get_nowait())
            except queue.Empty:
                break
        return lines[-1] if lines else ""

    def close(self) -> None:
        with self._lock:
            process = self._process
            stderr_reader = self._stderr_reader
            self._process = None
            self._endpoint = None
            self._stderr_reader = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write('{"cmd":"shutdown"}\n')
                process.stdin.flush()
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.terminate()
            try:
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        if stderr_reader is not None and stderr_reader is not threading.current_thread():
            stderr_reader.join(timeout=2)
        diagnostic_event(
            self.settings,
            "audio_capture",
            "stopped",
            returncode=process.poll(),
        )
