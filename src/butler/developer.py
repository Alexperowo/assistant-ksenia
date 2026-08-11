from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from butler.config import Settings


class DeveloperError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    cwd: str
    return_code: int
    output: str
    timed_out: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "return_code": self.return_code,
            "output": self.output,
            "timed_out": self.timed_out,
        }


class DeveloperRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        config = settings.raw.get("developer", {})
        raw_workspace = Path(str(config.get("workspace_dir", ".")))
        self.workspace_root = (
            raw_workspace.resolve()
            if raw_workspace.is_absolute()
            else (settings.root / raw_workspace).resolve()
        )
        self.allowed_programs = {
            str(item).casefold()
            for item in config.get(
                "allowed_programs",
                ["python", "python.exe", "py", "pytest", "git", "node", "npm", "npm.cmd"],
            )
        }
        self.timeout = max(5, int(config.get("command_timeout_seconds", 180)))
        self.max_output = max(2000, int(config.get("max_output_chars", 30000)))

    def _cwd(self, raw: object) -> Path:
        candidate = Path(str(raw or "."))
        candidate = candidate.resolve() if candidate.is_absolute() else (self.workspace_root / candidate).resolve()
        if candidate != self.workspace_root and self.workspace_root not in candidate.parents:
            raise DeveloperError("Рабочий каталог находится вне разрешённой папки проекта.")
        if not candidate.is_dir():
            raise DeveloperError(f"Рабочий каталог не найден: {candidate}")
        return candidate

    def run(self, command: object, cwd: object = ".") -> CommandResult:
        if isinstance(command, list):
            arguments = [str(item) for item in command]
        else:
            arguments = shlex.split(str(command), posix=False)
        if not arguments:
            raise DeveloperError("Команда не указана.")
        program = Path(arguments[0].strip('"')).name.casefold()
        if program not in self.allowed_programs:
            raise DeveloperError(f"Программа не разрешена для агента: {program}")
        directory = self._cwd(cwd)
        self._validate_arguments(program, arguments[1:], directory)
        try:
            completed = subprocess.run(
                arguments,
                cwd=str(directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = completed.stdout or ""
            if len(output) > self.max_output:
                output = output[-self.max_output :]
                output = "[Начало вывода сокращено]\n" + output
            return CommandResult(arguments, str(directory), completed.returncode, output)
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return CommandResult(arguments, str(directory), -1, output[-self.max_output :], True)
        except OSError as exc:
            raise DeveloperError(f"Не удалось запустить команду: {exc}") from exc

    def _validate_arguments(self, program: str, arguments: list[str], cwd: Path) -> None:
        lowered = [item.casefold() for item in arguments]
        if program in {"python", "python.exe", "py"}:
            if "-c" in lowered:
                raise DeveloperError("Inline-код Python запрещён. Используйте файл внутри проекта.")
            if "-m" in lowered:
                position = lowered.index("-m")
                module = lowered[position + 1] if position + 1 < len(lowered) else ""
                if module not in {"unittest", "pytest", "compileall"}:
                    raise DeveloperError(f"Модуль Python не разрешён: {module}")
            for argument in arguments:
                if argument.casefold().endswith(".py"):
                    script = Path(argument.strip('"'))
                    script = script.resolve() if script.is_absolute() else (cwd / script).resolve()
                    if script != self.workspace_root and self.workspace_root not in script.parents:
                        raise DeveloperError("Python-скрипт находится вне проекта.")
        elif program == "node":
            if any(item in {"-e", "--eval", "-p", "--print"} for item in lowered):
                raise DeveloperError("Inline-код Node.js запрещён.")
        elif program == "git":
            if any(item in {"-c", "--git-dir", "--work-tree"} for item in lowered):
                raise DeveloperError("Изменение области Git запрещено.")
            subcommand = next((item for item in lowered if not item.startswith("-")), "")
            denied = {"clean", "reset", "checkout", "restore", "push", "remote", "clone"}
            if subcommand in denied:
                raise DeveloperError(f"Опасная команда Git запрещена агенту: {subcommand}")
        elif program in {"npm", "npm.cmd"}:
            subcommand = next((item for item in lowered if not item.startswith("-")), "")
            if subcommand not in {"test", "run"}:
                raise DeveloperError("Разрешены только npm test и npm run.")

    def run_tests(self, cwd: object = ".") -> CommandResult:
        configured = self.settings.raw.get("developer", {}).get("test_command")
        command = configured or ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
        return self.run(command, cwd)
