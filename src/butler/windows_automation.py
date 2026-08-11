from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from butler.config import Settings


class WindowsAutomationError(RuntimeError):
    pass


class WindowsAutomation:
    """Runs UI Automation in an isolated worker to contain COM failures."""

    def __init__(self, settings: Settings) -> None:
        voice = settings.raw.get("voice", {})
        self.python = Path(str(voice.get("python", "")))
        self.worker = settings.root / "scripts" / "windows_uia_worker.py"
        self.timeout = int(
            settings.raw.get("windows", {}).get("automation_timeout_seconds", 20)
        )

    def execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.python.is_file():
            raise WindowsAutomationError(f"Не найден Python Windows-моста: {self.python}")
        if not self.worker.is_file():
            raise WindowsAutomationError(f"Не найден Windows-мост: {self.worker}")
        request = {"operation": operation, **payload}
        try:
            completed = subprocess.run(
                [str(self.python), "-u", str(self.worker)],
                input=json.dumps(request, ensure_ascii=False),
                cwd=str(self.worker.parent.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise WindowsAutomationError("Windows-приложение не ответило вовремя.") from exc
        except OSError as exc:
            raise WindowsAutomationError(f"Не удалось запустить Windows-мост: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise WindowsAutomationError(detail or "Windows-мост завершился с ошибкой.")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise WindowsAutomationError("Windows-мост вернул повреждённый ответ.") from exc
        if not isinstance(result, dict):
            raise WindowsAutomationError("Windows-мост вернул ответ неверного формата.")
        return result
