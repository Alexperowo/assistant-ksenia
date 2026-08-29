from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from butler.atomic_io import atomic_write_text, exclusive_file_lock
from butler.config import ModelProfile, Settings, reasoning_arguments
from butler.diagnostics import event as diagnostic_event
from butler.local_auth import api_key_file, local_api_key
from butler.processes import process_image_path, terminate_verified_process


class ModelManagerError(RuntimeError):
    pass


MANAGED_SERVER_OPTIONS = frozenset(
    {
        "--",
        "-m",
        "-c",
        "-ngl",
        "--model",
        "--model-url",
        "--model-draft",
        "--model-vocoder",
        "--hf-repo",
        "--hf-file",
        "--hf-token",
        "--mmproj",
        "--host",
        "--port",
        "--api-key",
        "--api-key-file",
        "--api-prefix",
        "--cors-origins",
        "--no-cors-credentials",
        "--ctx-size",
        "--n-gpu-layers",
        "--gpu-layers",
        "--spec-type",
        "--spec-draft-n-max",
        "--gpu-layers-draft",
        "--reasoning",
        "--reasoning-budget",
    }
)


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

    def _server_for(self, profile: ModelProfile) -> Path:
        if profile.backend is None:
            return self.settings.llama_server
        return profile.backend.executable

    @property
    def _integrity_cache_path(self) -> Path:
        return self.settings.runtime_dir / "models" / "integrity.json"

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _verify_artifact_integrity(
        self,
        *,
        role: str,
        artifact: str,
        path: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        if not expected_size or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ModelManagerError(
                f"Артефакт {artifact} профиля {role} не имеет полного size/SHA-256 lock."
            )
        stat = path.stat()
        signature = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "expected_sha256": expected_sha256,
        }
        cache_path = self._integrity_cache_path
        cache_key = os.path.normcase(str(path.resolve()))
        with exclusive_file_lock(cache_path, timeout=30.0):
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(cache, dict):
                    cache = {}
            except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
                cache = {}
            if cache.get(cache_key) == signature:
                return
            actual_sha256 = self._sha256_file(path)
            if actual_sha256 != expected_sha256:
                diagnostic_event(
                    self.settings,
                    "model_manager",
                    "model_hash_mismatch",
                    level="error",
                    role=role,
                    artifact=artifact,
                    model_file=path.name,
                )
                raise ModelManagerError(
                    f"SHA-256 артефакта {artifact} не совпал с lock-конфигурацией: "
                    f"{path}. Профиль не запущен."
                )
            cache[cache_key] = signature
            atomic_write_text(
                cache_path,
                json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True),
            )

    def build_command(self, profile: ModelProfile) -> list[str]:
        for argument in profile.extra_args:
            option = argument.split("=", 1)[0].casefold()
            if option in MANAGED_SERVER_OPTIONS:
                raise ModelManagerError(
                    f"extra_args профиля {profile.role} пытается переопределить "
                    f"управляемый параметр llama.cpp: {option}"
                )
        # Create the key before llama.cpp opens its key file during startup.
        local_api_key(self.settings)
        server = self._server_for(profile)
        command = [
            str(server),
            "--model",
            str(profile.model_path),
        ]
        uses_draft = profile.acceleration_type in {"draft-dflash", "draft-dspark"}
        if uses_draft and profile.draft_model_path is not None:
            command.extend(["--model-draft", str(profile.draft_model_path)])
        if profile.projector_path is not None:
            command.extend(["--mmproj", str(profile.projector_path)])
        command.extend(
            [
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
            ]
        )
        if profile.backend is None or profile.backend.cors_controls:
            command.extend(
                ["--cors-origins", "localhost", "--no-cors-credentials"]
            )
        if profile.acceleration_type != "none":
            command.extend(["--spec-type", profile.acceleration_type])
        if profile.acceleration_max_tokens:
            command.extend(
                ["--spec-draft-n-max", str(profile.acceleration_max_tokens)]
            )
        if uses_draft and profile.draft_model_path is not None and profile.draft_gpu_layers:
            command.extend(
                ["--gpu-layers-draft", str(profile.draft_gpu_layers)]
            )
        command.extend(profile.extra_args)
        command.extend(reasoning_arguments(profile.reasoning))
        return command

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
        with exclusive_file_lock(self.settings.state_file):
            atomic_write_text(
                self.settings.state_file,
                json.dumps(asdict(state), ensure_ascii=False, indent=2),
            )

    def _remove_state_if_unchanged(self, stale: RuntimeState) -> bool:
        with exclusive_file_lock(self.settings.state_file):
            if self._read_state() != stale:
                return False
            try:
                self.settings.state_file.unlink()
                return True
            except FileNotFoundError:
                return True
            except OSError:
                return False

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
            state_removed = self._remove_state_if_unchanged(state)
            diagnostic_event(
                self.settings,
                "model_manager",
                "stale_state_detected",
                level="warning",
                role=state.role,
                state_pid=state.pid,
                expected_executable=expected,
                actual_executable=actual,
                state_removed=state_removed,
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
        server = self._server_for(profile)
        started = time.monotonic()
        diagnostic_event(
            self.settings,
            "model_manager",
            "start_requested",
            role=role,
            model_file=profile.model_path.name,
            requested_context=profile.context_size,
            gpu_layers=profile.gpu_layers,
            acceleration=profile.acceleration_type,
            has_projector=profile.projector_path is not None,
            reasoning=profile.reasoning,
            wait=wait,
            backend=(profile.backend.name if profile.backend is not None else "default"),
        )
        if not profile.enabled:
            diagnostic_event(
                self.settings, "model_manager", "profile_disabled", level="error", role=role
            )
            raise ModelManagerError(f"Профиль «{role}» отключён в конфигурации.")
        if not server.is_file():
            raise ModelManagerError(f"Не найден llama-server backend-а профиля {role}: {server}")
        artifacts = (
            (
                "model",
                profile.model_path,
                profile.expected_size_bytes,
                profile.sha256,
            ),
            (
                "draft",
                (
                    profile.draft_model_path
                    if profile.acceleration_type in {"draft-dflash", "draft-dspark"}
                    else None
                ),
                profile.draft_expected_size_bytes,
                profile.draft_sha256,
            ),
            (
                "projector",
                profile.projector_path,
                profile.projector_expected_size_bytes,
                profile.projector_sha256,
            ),
        )
        for artifact, path, expected_size, expected_sha256 in artifacts:
            if path is None:
                continue
            if not path.is_file():
                raise ModelManagerError(f"Не найден артефакт модели {artifact}: {path}")
            actual_size = path.stat().st_size
            if expected_size and actual_size != expected_size:
                diagnostic_event(
                    self.settings,
                    "model_manager",
                    "model_size_mismatch",
                    level="error",
                    role=role,
                    artifact=artifact,
                    model_file=path.name,
                    expected_size_bytes=expected_size,
                    actual_size_bytes=actual_size,
                )
                raise ModelManagerError(
                    f"Артефакт {artifact} имеет неверный размер: {path}. "
                    f"Ожидалось {expected_size} байт, получено {actual_size}. "
                    "Профиль не запущен."
                )
            self._verify_artifact_integrity(
                role=role,
                artifact=artifact,
                path=path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
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
                cwd=str(server.parent),
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
            executable=str(server.resolve()),
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
            terminate_verified_process(process.pid, server)
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
