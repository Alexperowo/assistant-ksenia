from __future__ import annotations

import json
import os
import sqlite3
import shutil
import subprocess
import tempfile
import ctypes
from dataclasses import dataclass
from pathlib import Path

from butler.config import Settings
from butler.handoff import RoleHandoffStore
from butler.knowledge import KnowledgeStore
from butler.model_manager import ModelManager
from butler.procedures import ProcedureLibrary
from butler.rag import HybridRagIndex


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _command_version(command: str, *args: str) -> str | None:
    executable = shutil.which(command)
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None
        return (result.stdout or result.stderr).strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return executable


def _physical_memory_gb() -> float | None:
    if not hasattr(ctypes, "windll"):
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_phys", ctypes.c_ulonglong),
            ("avail_phys", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("avail_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("avail_virtual", ctypes.c_ulonglong),
            ("avail_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return round(status.total_phys / (1024**3), 1)


def model_check_required(enabled: bool, *, installation_mode: bool = False) -> bool:
    return bool(enabled and not installation_mode)


def run_checks(settings: Settings, *, installation_mode: bool = False) -> list[Check]:
    checks: list[Check] = []
    python_version = _command_version("py", "-3.12", "--version")
    configured_python = str(settings.raw.get("voice", {}).get("python", ""))
    if python_version is None and configured_python:
        try:
            result = subprocess.run(
                [configured_python, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode == 0:
                python_version = (result.stdout or result.stderr).strip() or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks.append(Check("Python 3.12", python_version is not None, python_version or "не найден"))

    voice = settings.raw.get("voice", {})
    voice_python = str(voice.get("python", ""))
    voice_version = None
    voice_packages = None
    voice_cuda = None
    voice_cuda_detail = None
    windows_packages = None
    browser_packages = None
    if voice_python:
        try:
            version_result = subprocess.run(
                [voice_python, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if version_result.returncode == 0:
                voice_version = (version_result.stdout or version_result.stderr).strip()
                package_result = subprocess.run(
                    [voice_python, "-c", "import torch, silero, vosk, sounddevice, faster_whisper"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                voice_packages = package_result.returncode == 0
                cuda_result = subprocess.run(
                    [
                        voice_python,
                        "-c",
                        "import torch; print(torch.__version__); "
                        "print(torch.cuda.is_available()); "
                        "print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA недоступна')",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                cuda_lines = [line.strip() for line in cuda_result.stdout.splitlines() if line.strip()]
                voice_cuda = cuda_result.returncode == 0 and len(cuda_lines) >= 2 and cuda_lines[1] == "True"
                voice_cuda_detail = ", ".join(cuda_lines) if cuda_lines else cuda_result.stderr.strip()
                windows_result = subprocess.run(
                    [voice_python, "-c", "import pywinauto, comtypes, win32api"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                windows_packages = windows_result.returncode == 0
                browser_result = subprocess.run(
                    [voice_python, "-c", "import playwright, googlenewsdecoder"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                browser_packages = browser_result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks.append(Check("Python голоса", bool(voice_version), voice_version or voice_python or "не настроен"))
    checks.append(
        Check(
            "Библиотеки голоса",
            voice_packages is True,
            "torch, silero, vosk, sounddevice, faster-whisper" if voice_packages else "не установлены или повреждены",
        )
    )
    checks.append(
        Check(
            "CUDA для распознавания речи",
            voice_cuda is True,
            voice_cuda_detail or "CUDA не проверена",
        )
    )
    checks.append(
        Check(
            "Управление Windows",
            windows_packages is True,
            "UI Automation, Win32 и мышь" if windows_packages else "pywinauto не установлен или повреждён",
        )
    )
    raw_wake_model = str(voice.get("wake_model", ""))
    wake_model = Path(raw_wake_model)
    if not wake_model.is_absolute():
        wake_model = settings.root / wake_model
    checks.append(Check("Модель распознавания", wake_model.is_dir(), str(wake_model)))
    whisper_model = Path(str(voice.get("stt_model", "")))
    checks.append(
        Check(
            "Whisper для диктовки",
            whisper_model.is_dir() and (whisper_model / "model.bin").is_file(),
            str(whisper_model) if whisper_model else "не настроен; используется Vosk",
        )
    )

    browser = settings.raw.get("browser", {})
    chromium = Path(str(browser.get("executable", "")))
    checks.append(
        Check(
            "Библиотеки браузера",
            browser_packages is True,
            "Playwright и декодер Google News"
            if browser_packages
            else "не установлены или повреждены",
        )
    )
    checks.append(Check("Chromium", chromium.is_file(), str(chromium)))
    procedures = ProcedureLibrary(settings.root).list()
    checks.append(
        Check(
            "Процедуры Ксении",
            len(procedures) >= 4,
            f"доступно {len(procedures)}: "
            + ", ".join(str(item.get("name", "")) for item in procedures),
        )
    )
    task_dir = settings.runtime_dir / "tasks"
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        task_ready = task_dir.is_dir()
    except OSError:
        task_ready = False
    checks.append(Check("Журнал задач", task_ready, str(task_dir)))
    try:
        knowledge_health = KnowledgeStore(settings.runtime_dir).health()
        checks.append(
            Check(
                "Долговременные факты",
                knowledge_health["integrity"] == "ok",
                f"{settings.runtime_dir / 'memory' / 'knowledge.sqlite3'}; "
                f"записей {knowledge_health['items']}; целостность {knowledge_health['integrity']}",
            )
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        checks.append(Check("Долговременные факты", False, str(exc)))
    try:
        handoff_health = RoleHandoffStore(settings.runtime_dir).health()
        checks.append(
            Check(
                "Память между ролями",
                handoff_health["integrity"] == "ok",
                f"{settings.runtime_dir / 'memory' / 'handoffs.sqlite3'}; "
                f"записей {handoff_health['items']}; целостность {handoff_health['integrity']}",
            )
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        checks.append(Check("Память между ролями", False, str(exc)))
    try:
        rag_health = HybridRagIndex(settings.runtime_dir).health()
        checks.append(
            Check(
                "Гибридный RAG-индекс",
                rag_health["integrity"] == "ok",
                f"документов {rag_health['documents']}, фрагментов {rag_health['chunks']}; "
                f"целостность {rag_health['integrity']}",
            )
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        checks.append(Check("Гибридный RAG-индекс", False, str(exc)))
    rag_config = settings.raw.get("rag", {})
    rag_model = Path(str(rag_config.get("model_path", "")))
    rag_enabled = bool(rag_config.get("enabled", False))
    rag_size = int(rag_config.get("expected_size_bytes", 0) or 0)
    rag_model_ready = rag_model.is_file() and (
        not rag_size or rag_model.stat().st_size == rag_size
    )
    checks.append(
        Check(
            "Модель RAG",
            rag_model_ready,
            (
                f"{rag_model}; CPU, порт {rag_config.get('port', 18081)}"
                if rag_enabled
                else f"выключена до живой проверки: {rag_model}"
            ),
            required=rag_enabled,
        )
    )

    diagnostics_path = settings.runtime_dir / "logs" / "diagnostics.jsonl"
    diagnostics_ready = False
    invalid_lines = 0
    line_count = 0
    try:
        diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_probe = tempfile.mkstemp(
            prefix=".diagnostics-write-test.",
            suffix=".tmp",
            dir=diagnostics_path.parent,
        )
        probe = Path(raw_probe)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write("ok")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            probe.unlink(missing_ok=True)
        diagnostics_ready = True
        if diagnostics_path.is_file():
            lines = diagnostics_path.read_text(encoding="utf-8").splitlines()
            line_count = len(lines)
            for line in lines:
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        invalid_lines += 1
                except json.JSONDecodeError:
                    invalid_lines += 1
    except OSError:
        diagnostics_ready = False
    checks.append(
        Check(
            "Диагностический журнал",
            diagnostics_ready and invalid_lines == 0,
            (
                f"{diagnostics_path}; записей {line_count}, повреждённых {invalid_lines}"
                if diagnostics_ready
                else f"нет доступа к {diagnostics_path.parent}"
            ),
        )
    )

    nvidia = _command_version(
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader",
    )
    checks.append(Check("NVIDIA", nvidia is not None, nvidia or "nvidia-smi не найден"))
    memory_gb = _physical_memory_gb()
    checks.append(
        Check(
            "Оперативная память",
            memory_gb is not None and memory_gb >= 15,
            (
                f"{memory_gb} ГБ установлено; пригодность проверяется "
                "по фактическим активным профилям"
                if memory_gb is not None
                else "не удалось определить"
            ),
        )
    )
    try:
        disk = shutil.disk_usage(settings.models_dir.anchor)
        free_gb = round(disk.free / (1024**3), 1)
        checks.append(
            Check(
                "Место для моделей",
                free_gb >= 10,
                f"{settings.models_dir.anchor}: свободно {free_gb} ГБ",
            )
        )
    except OSError:
        checks.append(Check("Место для моделей", False, str(settings.models_dir)))
    for backend_name in settings.engine_backend_names():
        backend = settings.engine_backend(backend_name)
        checks.append(
            Check(
                f"LLM backend: {backend_name}",
                backend.executable.is_file(),
                str(backend.executable),
                required=False,
            )
        )
    for role in settings.model_roles():
        profile = settings.model(role)
        artifacts = [
            ("model", profile.model_path, profile.expected_size_bytes),
        ]
        if profile.acceleration_type in {"draft-dflash", "draft-dspark"}:
            artifacts.append(
                ("draft", profile.draft_model_path, profile.draft_expected_size_bytes)
            )
        if profile.projector_path is not None:
            artifacts.append(
                (
                    "projector",
                    profile.projector_path,
                    profile.projector_expected_size_bytes,
                )
            )
        details = []
        model_size_ok = True
        for name, path, expected_size in artifacts:
            exists = path is not None and path.is_file()
            size_ok = bool(
                exists
                and (
                    not expected_size
                    or path.stat().st_size == expected_size
                )
            )
            model_size_ok = model_size_ok and size_ok
            if path is None:
                details.append(f"{name}: не настроен")
            elif not exists:
                details.append(f"{name}: не найден {path}")
            elif not size_ok:
                details.append(
                    f"{name}: размер {path.stat().st_size}, ожидалось {expected_size}"
                )
            else:
                details.append(f"{name}: {path.name}")
        checks.append(
            Check(
                f"Модель {role}",
                model_size_ok,
                f"{profile.label}: " + "; ".join(details),
                required=model_check_required(
                    profile.enabled, installation_mode=installation_mode
                ),
            )
        )
    active_services: list[tuple[str, ModelManager, object]] = []
    for service_name in settings.model_service_names():
        service_manager = ModelManager(settings, service_name)
        service_state = service_manager.running_state()
        if service_state is not None:
            active_services.append((service_name, service_manager, service_state))
    running_details = []
    all_ready = True
    for service_name, service_manager, service_state in active_services:
        ready = service_manager.api_ready()
        all_ready = all_ready and ready
        running_details.append(
            f"{service_name}: роль={service_state.role}, PID={service_state.pid}, "
            f"контекст={service_state.actual_context or service_manager.model_metadata().get('n_ctx', '?')} "
            f"из запрошенных {service_state.requested_context or settings.model(service_state.role).context_size}"
        )
    checks.append(
        Check(
            "Серверы моделей",
            bool(active_services and all_ready),
            "; ".join(running_details) if running_details else "не запущены",
            required=False,
        )
    )
    for service_name, service_manager, service_state in active_services:
        if not service_manager.api_ready():
            continue
        actual_context = service_state.actual_context or int(
            service_manager.model_metadata().get("n_ctx", 0) or 0
        )
        requested_context = (
            service_state.requested_context
            or settings.model(service_state.role).context_size
        )
        checks.append(
            Check(
                f"Контекст модели {service_state.role}",
                actual_context >= requested_context,
                f"сервис={service_name}, фактически {actual_context}, запрошено {requested_context}",
                required=False,
            )
        )
    return checks
