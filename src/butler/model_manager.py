from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from butler.config import ModelProfile, Settings, reasoning_arguments
from butler.diagnostics import event as diagnostic_event
from butler.local_auth import api_key_file, local_api_key
from butler.processes import process_image_path, terminate_verified_process


class ModelManagerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeState:
    pid: int
    role: str
    executable: str
    model: str
    started_at: str
    launch_signature: str = ""
    requested_context: int = 0
    actual_context: int = 0
    log_path: str = ""


class ModelManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_command(self, profile: ModelProfile) -> list[str]:
        # Create the key before llama.cpp opens its key file during startup.
        local_api_key(self.settings)
        return [
            str(self.settings.llama_server),
            "--model",
            str(profile.model_path),
            "--host",
            self.settings.host,
            "--port",
            str(self.settings.port),
            "--ctx-size",
            str(profile.context_size),
            "--n-gpu-layers",
            str(profile.gpu_layers),
            "--api-key-file",
            str(api_key_file(self.settings)),
            "--cors-origins",
            "localhost",
            "--no-cors-credentials",
            *profile.extra_args,
            *reasoning_arguments(profile.reasoning),
        ]

    def launch_signature(self, profile: ModelProfile) -> str:
        payload = json.dumps(self.build_command(profile), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_state(self) -> RuntimeState | None:
        try:
            raw = json.loads(self.settings.state_file.read_text(encoding="utf-8"))
            return RuntimeState(**raw)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None

    def _write_state(self, state: RuntimeState) -> None:
        self.settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.settings.state_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.settings.state_file)

    def _prune_logs(self, logs: Path, role: str) -> None:
        config = self.settings.raw.get("diagnostics", {})
        keep = max(3, min(int(config.get("llama_logs_per_role", 20)), 100))
        candidates = sorted(
            logs.glob(f"llama-{role}-*.log"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for old_log in candidates[keep:]:
            try:
                old_log.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            diagnostic_event(
                self.settings,
                "model_manager",
                "old_logs_pruned",
                role=role,
                removed_count=removed,
                retained_count=keep,
            )

    def running_state(self) -> RuntimeState | None:
        state = self._read_state()
        if state is None:
            return None
        actual = process_image_path(state.pid)
        expected = Path(state.executable).resolve()
        if actual != expected:
            diagnostic_event(
                self.settings,
                "model_manager",
                "stale_state_detected",
                level="warning",
                role=state.role,
                state_pid=state.pid,
                expected_executable=expected,
                actual_executable=actual,
            )
            return None
        return state

    def api_ready(self, timeout: float = 1.0) -> bool:
        url = f"http://{self.settings.host}:{self.settings.port}/health"
        try:
            request = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {local_api_key(self.settings)}"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def _port_open(self, timeout: float = 0.5) -> bool:
        host = self.settings.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        try:
            with socket.create_connection((host, self.settings.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _wait_port_closed(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._port_open(timeout=0.1):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True

    def model_metadata(self, timeout: float = 2.0) -> dict:
        url = f"http://{self.settings.host}:{self.settings.port}/v1/models"
        try:
            request = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {local_api_key(self.settings)}"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return {}
        data = value.get("data", []) if isinstance(value, dict) else []
        if not data or not isinstance(data[0], dict):
            return {}
        meta = data[0].get("meta", {})
        return meta if isinstance(meta, dict) else {}

    def is_current(self, role: str) -> bool:
        profile = self.settings.model(role)
        current = self.running_state()
        return bool(
            current
            and current.role == role
            and current.launch_signature == self.launch_signature(profile)
            and self.api_ready()
        )

    def start(self, role: str, wait: bool = True) -> RuntimeState:
        profile = self.settings.model(role)
        started = time.monotonic()
        diagnostic_event(
            self.settings,
            "model_manager",
            "start_requested",
            role=role,
            model_file=profile.model_path.name,
            requested_context=profile.context_size,
            gpu_layers=profile.gpu_layers,
            reasoning=profile.reasoning,
            wait=wait,
        )
        if not profile.enabled:
            diagnostic_event(
                self.settings, "model_manager", "profile_disabled", level="error", role=role
            )
            raise ModelManagerError(f"Профиль «{role}» отключён в конфигурации.")
        if not self.settings.llama_server.is_file():
            raise ModelManagerError(f"Не найден llama-server: {self.settings.llama_server}")
        if not profile.model_path.is_file():
            raise ModelManagerError(f"Не найден файл модели: {profile.model_path}")
        if (
            profile.expected_size_bytes
            and profile.model_path.stat().st_size != profile.expected_size_bytes
        ):
            actual_size = profile.model_path.stat().st_size
            diagnostic_event(
                self.settings,
                "model_manager",
                "model_size_mismatch",
                level="error",
                role=role,
                model_file=profile.model_path.name,
                expected_size_bytes=profile.expected_size_bytes,
                actual_size_bytes=actual_size,
            )
            raise ModelManagerError(
                f"Файл модели имеет неверный размер: {profile.model_path}. "
                f"Ожидалось {profile.expected_size_bytes} байт, получено {actual_size}. "
                "Модель не запущена."
            )

        current = self.running_state()
        signature = self.launch_signature(profile)
        if (
            current
            and current.role == role
            and current.launch_signature == signature
            and self.api_ready()
        ):
            diagnostic_event(
                self.settings,
                "model_manager",
                "reused_running_model",
                role=role,
                model_pid=current.pid,
                actual_context=current.actual_context,
            )
            return current
        if current:
            if not self.stop() or not self._wait_port_closed():
                raise ModelManagerError(
                    "Предыдущая локальная модель не освободила порт после остановки. "
                    "Новый процесс не запущен."
                )

        # A healthy response is not enough to prove ownership: another local
        # service could already be listening on the configured port. Refuse to
        # launch instead of mistaking that service for the process below.
        if self._port_open():
            diagnostic_event(
                self.settings,
                "model_manager",
                "unknown_port_owner",
                level="error",
                host=self.settings.host,
                port=self.settings.port,
                role=role,
            )
            raise ModelManagerError(
                f"Порт локальной модели {self.settings.host}:{self.settings.port} уже занят "
                "неизвестным процессом. Ксения не будет его останавливать."
            )

        logs = self.settings.runtime_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self._prune_logs(logs, role)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = logs / f"llama-{role}-{stamp}.log"
        log_file = log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                self.build_command(profile),
                cwd=str(self.settings.llama_server.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
            )
        finally:
            log_file.close()
        diagnostic_event(
            self.settings,
            "model_manager",
            "process_started",
            role=role,
            model_pid=process.pid,
            log_path=log_path,
        )

        state = RuntimeState(
            pid=process.pid,
            role=role,
            executable=str(self.settings.llama_server.resolve()),
            model=str(profile.model_path),
            started_at=datetime.now(timezone.utc).isoformat(),
            launch_signature=signature,
            requested_context=profile.context_size,
            actual_context=0,
            log_path=str(log_path),
        )
        self._write_state(state)

        if wait:
            deadline = time.monotonic() + self.settings.startup_timeout_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    try:
                        self.settings.state_file.unlink()
                    except FileNotFoundError:
                        pass
                    diagnostic_event(
                        self.settings,
                        "model_manager",
                        "startup_process_exited",
                        level="error",
                        role=role,
                        model_pid=process.pid,
                        returncode=process.returncode,
                        duration_ms=round((time.monotonic() - started) * 1000),
                        log_path=log_path,
                    )
                    raise ModelManagerError(
                        f"llama-server завершился с кодом {process.returncode}. Журнал: {log_path}"
                    )
                if self.api_ready():
                    metadata = self.model_metadata()
                    actual_context = int(metadata.get("n_ctx", 0) or 0)
                    state = RuntimeState(
                        **{
                            **asdict(state),
                            "actual_context": actual_context,
                        }
                    )
                    self._write_state(state)
                    diagnostic_event(
                        self.settings,
                        "model_manager",
                        "ready",
                        role=role,
                        model_pid=process.pid,
                        requested_context=profile.context_size,
                        actual_context=actual_context,
                        duration_ms=round((time.monotonic() - started) * 1000),
                    )
                    return state
                time.sleep(0.5)
            terminate_verified_process(process.pid, self.settings.llama_server)
            try:
                self.settings.state_file.unlink()
            except FileNotFoundError:
                pass
            diagnostic_event(
                self.settings,
                "model_manager",
                "startup_timeout",
                level="error",
                role=role,
                model_pid=process.pid,
                duration_ms=round((time.monotonic() - started) * 1000),
                log_path=log_path,
            )
            raise ModelManagerError(
                f"llama-server не стал готов за {self.settings.startup_timeout_seconds} секунд. "
                f"Журнал: {log_path}"
            )
        diagnostic_event(
            self.settings,
            "model_manager",
            "start_returned_without_wait",
            role=role,
            model_pid=process.pid,
        )
        return state

    def stop(self) -> bool:
        state = self.running_state()
        if state is None:
            diagnostic_event(self.settings, "model_manager", "stop_no_running_model")
            return False
        started = time.monotonic()
        diagnostic_event(
            self.settings,
            "model_manager",
            "stop_requested",
            role=state.role,
            model_pid=state.pid,
        )
        stopped = terminate_verified_process(state.pid, Path(state.executable))
        if stopped:
            try:
                self.settings.state_file.unlink()
            except FileNotFoundError:
                pass
        diagnostic_event(
            self.settings,
            "model_manager",
            "stop_completed" if stopped else "stop_rejected",
            level="info" if stopped else "error",
            role=state.role,
            model_pid=state.pid,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return stopped

    def switch(self, role: str) -> RuntimeState:
        diagnostic_event(self.settings, "model_manager", "switch_requested", role=role)
        self.stop()
        return self.start(role)
