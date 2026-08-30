from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

from butler.audio_capture import CaptureEndpoint
from butler.config import Settings
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception


class WakeListenerError(RuntimeError):
    pass


class WakeListenerTimeout(WakeListenerError):
    """Normal end of one idle listening window, not a microphone failure."""


class WakeListenerCancelled(WakeListenerError):
    """The owner no longer needs the temporary wake listener."""


class _AnyCancellationEvent:
    """Minimal Event-compatible view that is set when any source is set."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class MicrophoneCaptureGate:
    """Hand one physical microphone between a monitor and exclusive capture.

    The background wake/stop listener owns the microphone most of the time. A
    confirmation recognizer must first cancel that listener and wait until its
    worker has fully closed the device. This gate makes the hand-off explicit
    instead of allowing two audio workers to race for the same endpoint.
    """

    def __init__(self) -> None:
        self._pause_requested = threading.Event()
        self._monitor_paused = threading.Event()
        self._exclusive_lock = threading.Lock()

    def monitor_cancel_event(
        self, owner_cancelled: threading.Event
    ) -> _AnyCancellationEvent:
        return _AnyCancellationEvent(owner_cancelled, self._pause_requested)

    def monitor_checkpoint(self, owner_cancelled: threading.Event) -> bool:
        """Acknowledge a requested hand-off and wait until capture is released."""

        if owner_cancelled.is_set():
            return False
        if not self._pause_requested.is_set():
            return True
        self._monitor_paused.set()
        try:
            while self._pause_requested.is_set() and not owner_cancelled.is_set():
                owner_cancelled.wait(0.05)
        finally:
            self._monitor_paused.clear()
        return not owner_cancelled.is_set()

    @contextmanager
    def exclusive_capture(self, timeout: float) -> Iterator[None]:
        if timeout <= 0:
            raise ValueError("Тайм-аут передачи микрофона должен быть положительным.")
        with self._exclusive_lock:
            self._pause_requested.set()
            try:
                if not self._monitor_paused.wait(timeout):
                    raise TimeoutError(
                        "Фоновый слушатель не освободил микрофон вовремя."
                    )
                yield
            finally:
                self._pause_requested.clear()


class WakeListener:
    def __init__(
        self,
        settings: Settings,
        capture_endpoint: CaptureEndpoint | None = None,
    ) -> None:
        self.settings = settings
        voice = settings.raw.get("voice", {})
        self.root = settings.root
        self.python = Path(str(voice.get("python", "")))
        self.worker = settings.root / "scripts" / "wake_worker.py"
        raw_model = Path(str(voice.get("wake_model", "")))
        self.model = raw_model if raw_model.is_absolute() else settings.root / raw_model
        self.phrase = str(voice.get("wake_word", "Ксения слушай"))
        self.sample_rate = int(voice.get("wake_sample_rate", 16000))
        self.device = str(voice.get("wake_device", ""))
        self.capture_endpoint = capture_endpoint

    def wait_event(
        self,
        timeout: int = 120,
        *,
        external_events: queue.Queue | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, object]:
        if cancel_event is not None and cancel_event.is_set():
            raise WakeListenerCancelled("Ожидание активации отменено владельцем.")
        if not self.python.is_file():
            raise WakeListenerError(f"Не найден голосовой Python: {self.python}")
        if not self.worker.is_file():
            raise WakeListenerError(f"Не найден слушатель: {self.worker}")
        if not self.model.is_dir():
            raise WakeListenerError(f"Не найдена модель активации: {self.model}")

        started = time.monotonic()
        diagnostic_event(
            self.settings,
            "wake",
            "listen_started",
            timeout_seconds=timeout,
            requested_device=self.device,
        )
        try:
            worker_env = (
                self.capture_endpoint.child_environment()
                if self.capture_endpoint is not None
                else os.environ.copy()
            )
            worker_env["PYTHONIOENCODING"] = "utf-8"
            worker_env["PYTHONUTF8"] = "1"
            command = [
                str(self.python),
                "-u",
                str(self.worker),
                "--model",
                str(self.model),
                "--phrase",
                self.phrase,
                "--sample-rate",
                str(self.sample_rate),
                "--device",
                self.device,
                "--timeout",
                str(timeout),
            ]
            if self.capture_endpoint is not None:
                command.extend(self.capture_endpoint.command_arguments())
            process = subprocess.Popen(
                command,
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=worker_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            diagnostic_exception(self.settings, "wake", "worker_start_failed", exc)
            raise WakeListenerError(f"Не удалось запустить слушатель: {exc}") from exc

        try:
            if process.stdout is None:
                raise WakeListenerError("Слушатель не вернул канал событий.")
            worker_lines: queue.Queue[str | None] = queue.Queue()

            def read_worker() -> None:
                for worker_line in process.stdout:
                    worker_lines.put(worker_line)
                worker_lines.put(None)

            threading.Thread(target=read_worker, daemon=True).start()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    diagnostic_event(
                        self.settings,
                        "wake",
                        "listen_cancelled",
                        duration_ms=round((time.monotonic() - started) * 1000),
                    )
                    raise WakeListenerCancelled(
                        "Ожидание активации отменено владельцем."
                    )
                if external_events is not None:
                    try:
                        external = external_events.get_nowait()
                    except queue.Empty:
                        external = None
                    if external is not None:
                        received_at = float(
                            external.get("received_at", 0.0)
                            if isinstance(external, dict)
                            else getattr(external, "received_at", 0.0)
                        )
                        if received_at and time.monotonic() - received_at > 2.0:
                            diagnostic_event(
                                self.settings,
                                "wake",
                                "stale_external_activation_discarded",
                                age_ms=round((time.monotonic() - received_at) * 1000),
                            )
                            continue
                        button = str(
                            external.get("name", "")
                            if isinstance(external, dict)
                            else getattr(external, "name", "")
                        )
                        vk_code = int(
                            external.get("vk_code", 0)
                            if isinstance(external, dict)
                            else getattr(external, "vk_code", 0)
                        )
                        diagnostic_event(
                            self.settings,
                            "wake",
                            "external_activation",
                            duration_ms=round((time.monotonic() - started) * 1000),
                            button=button,
                            vk_code=vk_code,
                        )
                        return {
                            "event": "headset",
                            "button": button,
                            "vk_code": vk_code,
                        }
                try:
                    line = worker_lines.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None:
                        raise WakeListenerError("Слушатель завершился без события.")
                    continue
                if line is None:
                    raise WakeListenerError("Слушатель завершился без события.")
                worker_event = json.loads(line)
                if worker_event.get("event") != "listening_ready":
                    break
                diagnostic_event(
                    self.settings,
                    "wake",
                    "microphone_ready",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    device=worker_event.get("device", ""),
                    host_api=worker_event.get("host_api", ""),
                    capture_rate=worker_event.get("capture_rate", 0),
                    device_index=worker_event.get("device_index", -1),
                    candidate_count=worker_event.get("candidate_count", 0),
                    failed_attempt_count=worker_event.get("failed_attempt_count", 0),
                    input_attempts=worker_event.get("input_attempts", []),
                )
            if worker_event.get("event") in {"wake", "stop"}:
                diagnostic_event(
                    self.settings,
                    "wake",
                    "listen_completed",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    wake_event=worker_event.get("event", ""),
                    transcript=worker_event.get("text", ""),
                    device=worker_event.get("device", ""),
                    host_api=worker_event.get("host_api", ""),
                    capture_rate=worker_event.get("capture_rate", 0),
                    device_index=worker_event.get("device_index", -1),
                    candidate_count=worker_event.get("candidate_count", 0),
                    failed_attempt_count=worker_event.get("failed_attempt_count", 0),
                    input_attempts=worker_event.get("input_attempts", []),
                )
                return worker_event
            if worker_event.get("event") == "timeout":
                diagnostic_event(
                    self.settings,
                    "wake",
                    "idle_timeout",
                    duration_ms=round((time.monotonic() - started) * 1000),
                    device=worker_event.get("device", ""),
                    host_api=worker_event.get("host_api", ""),
                    capture_rate=worker_event.get("capture_rate", 0),
                    device_index=worker_event.get("device_index", -1),
                    candidate_count=worker_event.get("candidate_count", 0),
                    failed_attempt_count=worker_event.get("failed_attempt_count", 0),
                    input_attempts=worker_event.get("input_attempts", []),
                )
                raise WakeListenerTimeout(
                    str(
                        worker_event.get(
                            "error",
                            "Истекло штатное время ожидания фразы активации.",
                        )
                    )
                )
            diagnostic_event(
                self.settings,
                "wake",
                "listen_failed",
                level="error",
                duration_ms=round((time.monotonic() - started) * 1000),
                error=worker_event.get("error", "Неизвестная ошибка микрофона."),
            )
            raise WakeListenerError(str(worker_event.get("error", "Неизвестная ошибка микрофона.")))
        except json.JSONDecodeError as exc:
            diagnostic_exception(
                self.settings,
                "wake",
                "invalid_worker_event",
                exc,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise WakeListenerError("Слушатель вернул повреждённое событие.") from exc
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stderr is not None:
                detail = process.stderr.read().strip()
                if detail:
                    lowered = detail.casefold()
                    is_error = any(
                        marker in lowered
                        for marker in ("traceback", "error:", "exception", "fatal")
                    )
                    diagnostic_event(
                        self.settings,
                        "wake",
                        "worker_stderr",
                        level="error" if is_error else "info",
                        detail=detail,
                    )

    def wait_once(self, timeout: int = 120) -> str:
        event = self.wait_event(timeout)
        if event.get("event") != "wake":
            raise WakeListenerError("Ожидалась фраза активации, но прозвучала команда остановки.")
        return str(event.get("text", self.phrase))
