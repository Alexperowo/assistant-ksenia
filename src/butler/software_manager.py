from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from butler.config import Settings
from butler.windows_bridge import activate_window, list_windows


class SoftwareManagerError(RuntimeError):
    pass


FORBIDDEN_COMMAND_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"\bformat(?:\.com|\.exe)?\s+[a-zA-Z]:",
        "Форматирование дисковых разделов категорически запрещено.",
    ),
    (
        r"\bdiskpart(?:\.exe)?\b",
        "Управление дисковыми разделами через diskpart запрещено.",
    ),
    (
        r"\bbcdedit(?:\.exe)?\b",
        "Модификация конфигурации загрузки Windows (bcdedit) запрещена.",
    ),
    (
        r"\bbootrec(?:\.exe)?\b",
        "Модификация загрузочных секторов Windows запрещена.",
    ),
    (
        r"\bvssadmin(?:\.exe)?\s+delete\b",
        "Удаление теневых копий томов Windows запрещено.",
    ),
    (
        r"\bfsutil(?:\.exe)?\s+usn\s+deletejournal\b",
        "Удаление журналов файловой системы запрещено.",
    ),
    (
        r"Set-MpPreference.*-DisableRealtimeMonitoring\s+[\$]?(?:true|1)\b",
        "Отключение защиты Windows Defender в реальном времени запрещено.",
    ),
    (
        r"netsh(?:\.exe)?\s+advfirewall\s+set\s+.*state\s+off\b",
        "Отключение сетевого экрана (брэндмауэра) Windows запрещено.",
    ),
    (
        r"reg(?:\.exe)?\s+(?:add|delete)\s+.*(?:DisableAntiSpyware|Policies\\Microsoft\\Windows\s+Defender)",
        "Модификация политик безопасности Windows Defender через реестр запрещена.",
    ),
    (
        r"(?:powershell|pwsh)(?:\.exe)?\s+.*(?:-[eE]|-[eE]nc(?:odedcommand)?)\s+",
        "Запуск закодированных (base64) скриптов PowerShell запрещён.",
    ),
    (
        r"\[System\.Convert\]::FromBase64String",
        "Выполнение скрытого base64-кода запрещено.",
    ),
    (
        r"certutil(?:\.exe)?\s+.*-decode\b",
        "Декодирование скрытых исполняемых файлов через certutil запрещено.",
    ),
    (
        r"\b(?:iex|Invoke-Expression)\s*\(",
        "Динамическое исполнение невалидированных строк через Invoke-Expression запрещено.",
    ),
    (
        r"net(?:\.exe)?\s+user\s+.*(?:/add|/delete)",
        "Создание или удаление учётных записей Windows через net user запрещено.",
    ),
    (
        r"net(?:\.exe)?\s+localgroup\s+administrators\s+.*(?:/add|/delete)",
        "Изменение состава локальной группы администраторов запрещено.",
    ),
    (
        r"(?:rmdir|rd)\s+.*[\\/](?:Windows|System32)\b",
        "Удаление системных каталогов Windows запрещено.",
    ),
    (
        r"Remove-Item\s+.*[\\/](?:Windows|System32)\b",
        "Удаление системных каталогов Windows через PowerShell запрещено.",
    ),
)


def validate_command_safety(command: str | list[str]) -> tuple[bool, str]:
    if isinstance(command, list):
        text = " ".join(str(item) for item in command)
    else:
        text = str(command or "")
    normalized = text.strip()
    if not normalized:
        return False, "Команда не указана."

    for pattern, reason in FORBIDDEN_COMMAND_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            return False, f"Команда заблокирована политикой безопасности: {reason}"

    return True, "Команда признана безопасной."


def is_public_download_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw or "\\" in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return False
    try:
        parsed = urllib.parse.urlparse(raw)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"} or not hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return False

    try:
        legacy_ipv4 = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(hostname)))
    except OSError:
        legacy_ipv4 = None
    if legacy_ipv4 is not None:
        return legacy_ipv4.is_global

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        return address.is_global

    return "." in hostname


def broadcast_environment_change() -> bool:
    if not hasattr(ctypes, "windll"):
        return False
    try:
        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x001A
        smto_abortifhung = 0x0002
        result = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast,
            wm_settingchange,
            0,
            "Environment",
            smto_abortifhung,
            5000,
            ctypes.byref(result),
        )
        return True
    except (OSError, AttributeError):
        return False


class SoftwareManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.downloads_dir = (settings.runtime_dir / "downloads").resolve()
        self.tools_dir = (settings.root / "tools").resolve()
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.tools_dir.mkdir(parents=True, exist_ok=True)

    def _run_subprocess(
        self,
        arguments: list[str],
        *,
        timeout: int = 300,
        cwd: Path | None = None,
    ) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                arguments,
                cwd=str(cwd) if cwd else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return completed.returncode, completed.stdout or ""
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return -1, output + f"\n[Таймаут выполнения команды: {timeout} с]"
        except FileNotFoundError:
            raise SoftwareManagerError(f"Утилита не найдена: {arguments[0]}") from None
        except OSError as exc:
            raise SoftwareManagerError(f"Ошибка запуска процесса: {exc}") from None

    # Tier 1: Windows Package Manager (winget)
    def search_winget(self, query: str, *, timeout: int = 25) -> list[dict[str, Any]]:
        clean_query = str(query or "").strip()
        if not clean_query:
            return []
        arguments = [
            "winget",
            "search",
            clean_query,
            "--source",
            "winget",
            "--accept-source-agreements",
        ]
        code, output = self._run_subprocess(arguments, timeout=timeout)
        if code != 0 and not output.strip():
            return []
        return self._parse_winget_table(output)

    def show_winget(self, package_id: str, *, timeout: int = 25) -> dict[str, Any]:
        clean_id = str(package_id or "").strip()
        if not clean_id:
            raise SoftwareManagerError("Не указан идентификатор пакета.")
        arguments = [
            "winget",
            "show",
            "--id",
            clean_id,
            "--source",
            "winget",
            "--accept-source-agreements",
        ]
        code, output = self._run_subprocess(arguments, timeout=timeout)
        if code != 0:
            raise SoftwareManagerError(
                f"Не удалось получить информацию о пакете {clean_id}: {output.strip() or 'пакет не найден'}"
            )
        return self._parse_winget_show(output, clean_id)

    def install_winget(
        self,
        package_id: str,
        *,
        silent: bool = True,
        timeout: int = 600,
    ) -> dict[str, Any]:
        clean_id = str(package_id or "").strip()
        if not clean_id:
            raise SoftwareManagerError("Не указан идентификатор пакета.")
        arguments = [
            "winget",
            "install",
            "--id",
            clean_id,
            "--source",
            "winget",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ]
        if silent:
            arguments.append("--silent")
        code, output = self._run_subprocess(arguments, timeout=timeout)
        ok = code == 0
        message = (
            f"Программа {clean_id} успешно установлена через winget."
            if ok
            else f"Установка {clean_id} завершилась с кодом {code}."
        )
        return {
            "tier": "winget",
            "package_id": clean_id,
            "ok": ok,
            "return_code": code,
            "message": message,
            "output": output[-4000:] if len(output) > 4000 else output,
        }

    def list_installed_winget(self, query: str = "", *, timeout: int = 25) -> list[dict[str, Any]]:
        arguments = ["winget", "list", "--source", "winget"]
        if query.strip():
            arguments.insert(2, query.strip())
        code, output = self._run_subprocess(arguments, timeout=timeout)
        if code != 0 and not output.strip():
            return []
        return self._parse_winget_table(output)

    @staticmethod
    def _parse_winget_table(output: str) -> list[dict[str, Any]]:
        lines = [line.rstrip() for line in output.splitlines() if line.strip()]
        if not lines:
            return []
        header_index = -1
        for i, line in enumerate(lines):
            if "Name" in line and "Id" in line:
                header_index = i
                break
        if header_index == -1 or header_index + 1 >= len(lines):
            return []

        header_line = lines[header_index]
        sep_line = lines[header_index + 1]
        if not all(c in "- " for c in sep_line):
            return []

        name_pos = header_line.find("Name")
        id_pos = header_line.find("Id")
        version_pos = header_line.find("Version")
        match_pos = header_line.find("Match")
        source_pos = header_line.find("Source")

        results: list[dict[str, Any]] = []
        for line in lines[header_index + 2:]:
            if len(line) <= id_pos:
                continue
            name = line[name_pos:id_pos].strip()
            if version_pos != -1 and len(line) > version_pos:
                pkg_id = line[id_pos:version_pos].strip()
                end_ver = match_pos if match_pos != -1 else (source_pos if source_pos != -1 else len(line))
                version = line[version_pos:end_ver].strip()
            else:
                pkg_id = line[id_pos:].strip()
                version = ""

            if pkg_id:
                results.append(
                    {
                        "id": pkg_id,
                        "name": name,
                        "version": version,
                        "source": "winget",
                    }
                )
        return results

    @staticmethod
    def _parse_winget_show(output: str, package_id: str) -> dict[str, Any]:
        info: dict[str, Any] = {"id": package_id}
        for line in output.splitlines():
            stripped = line.strip()
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                clean_key = key.strip().casefold().replace(" ", "_")
                clean_val = val.strip()
                if clean_val and clean_key not in info:
                    info[clean_key] = clean_val
        return info

    # Tier 2: Direct Download & Integrity Check
    def download_installer(
        self,
        url: str,
        *,
        expected_sha256: str | None = None,
        target_filename: str | None = None,
        timeout: int = 120,
        max_bytes: int = 524_288_000,
    ) -> dict[str, Any]:
        clean_url = str(url or "").strip()
        if not is_public_download_url(clean_url):
            raise SoftwareManagerError(
                "Недопустимый URL для скачивания: разрешены только публичные HTTP/HTTPS адреса."
            )

        parsed = urllib.parse.urlparse(clean_url)
        default_name = Path(parsed.path).name or "installer.exe"
        filename = re.sub(r'[^a-zA-Z0-9_.-]', '_', target_filename or default_name)
        if not (filename.casefold().endswith(".exe") or filename.casefold().endswith(".msi") or filename.casefold().endswith(".zip")):
            filename += ".exe"

        dest_path = self.downloads_dir / filename
        hasher = hashlib.sha256()
        total_bytes = 0

        req = urllib.request.Request(
            clean_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KseniaButler/1.0"},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                with open(dest_path, "wb") as outfile:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            dest_path.unlink(missing_ok=True)
                            raise SoftwareManagerError(
                                f"Файл превысил допустимый лимит размера ({max_bytes // 1_048_576} МБ)."
                            )
                        hasher.update(chunk)
                        outfile.write(chunk)
        except urllib.error.URLError as exc:
            dest_path.unlink(missing_ok=True)
            raise SoftwareManagerError(f"Сетевая ошибка при загрузке: {exc}") from None
        except OSError as exc:
            dest_path.unlink(missing_ok=True)
            raise SoftwareManagerError(f"Ошибка записи скачиваемого файла: {exc}") from None

        computed_sha256 = hasher.hexdigest()
        verified = True
        if expected_sha256:
            expected_clean = expected_sha256.strip().casefold()
            if computed_sha256.casefold() != expected_clean:
                dest_path.unlink(missing_ok=True)
                raise SoftwareManagerError(
                    f"Контрольная сумма SHA-256 не совпала!\n"
                    f"Ожидалось: {expected_clean}\n"
                    f"Получено:   {computed_sha256}"
                )

        return {
            "tier": "download",
            "path": str(dest_path),
            "filename": filename,
            "size_bytes": total_bytes,
            "sha256": computed_sha256,
            "verified": verified,
        }

    def install_downloaded(
        self,
        installer_path: Path | str,
        *,
        silent: bool = True,
        custom_args: list[str] | None = None,
        timeout: int = 600,
    ) -> dict[str, Any]:
        path = Path(installer_path).resolve()
        if not path.is_file():
            raise SoftwareManagerError(f"Файл установщика не найден: {path}")

        suffix = path.suffix.casefold()
        if suffix not in {".exe", ".msi"}:
            raise SoftwareManagerError(
                f"Неподдерживаемый формат инсталлятора: {suffix}. Разрешены .exe и .msi."
            )

        if custom_args:
            is_safe, reason = validate_command_safety(custom_args)
            if not is_safe:
                raise SoftwareManagerError(reason)

        if suffix == ".msi":
            arguments = ["msiexec.exe", "/i", str(path)]
            if silent:
                arguments.extend(["/qn", "/norestart"])
            if custom_args:
                arguments.extend(custom_args)
        else:
            arguments = [str(path)]
            if custom_args:
                arguments.extend(custom_args)
            elif silent:
                arguments.append("/S")

        code, output = self._run_subprocess(arguments, timeout=timeout)
        ok = code == 0
        message = (
            f"Установщик {path.name} выполнен успешно."
            if ok
            else f"Установщик {path.name} завершился с кодом {code}."
        )
        return {
            "tier": "download",
            "path": str(path),
            "ok": ok,
            "return_code": code,
            "message": message,
            "output": output[-4000:] if len(output) > 4000 else output,
        }

    # Tier 3: Interactive UI Automation Helper
    def prepare_interactive_installer(
        self,
        installer_path: Path | str,
        *,
        wait_seconds: float = 3.0,
    ) -> dict[str, Any]:
        path = Path(installer_path).resolve()
        if not path.is_file():
            raise SoftwareManagerError(f"Файл установщика не найден: {path}")

        suffix = path.suffix.casefold()
        if suffix not in {".exe", ".msi"}:
            raise SoftwareManagerError(
                f"Неподдерживаемый формат инсталлятора: {suffix}. Разрешены .exe и .msi."
            )

        arguments = ["msiexec.exe", "/i", str(path)] if suffix == ".msi" else [str(path)]
        try:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
        except OSError as exc:
            raise SoftwareManagerError(f"Не удалось запустить процесс мастера установки: {exc}") from None

        time.sleep(max(0.5, wait_seconds))

        matching_handle = 0
        matching_title = ""
        try:
            visible = list_windows()
            for win in visible:
                if int(win.get("process_id", 0)) == process.pid:
                    matching_handle = int(win.get("handle", 0))
                    matching_title = str(win.get("title", ""))
                    break
        except (OSError, RuntimeError):
            pass

        if matching_handle:
            try:
                activate_window(matching_handle)
            except (OSError, RuntimeError):
                pass

        return {
            "tier": "interactive",
            "pid": process.pid,
            "handle": matching_handle,
            "window_title": matching_title,
            "installer_path": str(path),
            "message": (
                f"Мастер установки {path.name} запущен (PID {process.pid}). "
                f"Окно: '{matching_title or 'не определено'}'. "
                "Готова к исследованию структуры окна через UI Automation."
            ),
        }

    # Tier 4: Portable Archives & Environment Management
    def install_portable_zip(
        self,
        zip_path: Path | str,
        *,
        target_name: str | None = None,
        add_to_path: bool = True,
    ) -> dict[str, Any]:
        path = Path(zip_path).resolve()
        if not path.is_file():
            raise SoftwareManagerError(f"Файл архива не найден: {path}")

        dir_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', target_name or path.stem)
        target_dir = (self.tools_dir / dir_name).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        extracted_count = 0
        try:
            with zipfile.ZipFile(path, "r") as zf:
                for member in zf.infolist():
                    member_path = (target_dir / member.filename).resolve()
                    if target_dir not in member_path.parents and member_path != target_dir:
                        raise SoftwareManagerError(
                            f"Небезопасный путь в архиве (zip-slip): {member.filename}"
                        )
                zf.extractall(target_dir)
                extracted_count = len(zf.infolist())
        except zipfile.BadZipFile:
            raise SoftwareManagerError(f"Повреждённый ZIP-архив: {path}") from None

        path_result = None
        if add_to_path:
            path_result = self.add_to_user_path(target_dir)

        return {
            "tier": "portable",
            "target_dir": str(target_dir),
            "files_extracted": extracted_count,
            "added_to_path": add_to_path,
            "path_result": path_result,
            "message": f"Архив распакован в {target_dir}. Файлов: {extracted_count}.",
        }

    @staticmethod
    def get_user_path() -> list[str]:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "Path")
                return [p.strip() for p in str(val).split(";") if p.strip()]
        except (OSError, ImportError):
            return []

    def add_to_user_path(self, directory: Path | str) -> dict[str, Any]:
        target = Path(directory).resolve()
        if not target.is_dir():
            raise SoftwareManagerError(f"Указанный каталог не существует: {target}")

        target_str = str(target)
        current = self.get_user_path()
        normalized_current = [p.casefold().rstrip("\\/") for p in current]
        if target_str.casefold().rstrip("\\/") in normalized_current:
            return {
                "ok": True,
                "already_present": True,
                "path": target_str,
                "message": f"Каталог {target_str} уже присутствует в переменной PATH пользователя.",
            }

        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                val, reg_type = winreg.QueryValueEx(key, "Path")
                clean_val = str(val).rstrip(";")
                new_val = f"{clean_val};{target_str}" if clean_val else target_str
                winreg.SetValueEx(key, "Path", 0, reg_type, new_val)
        except (OSError, ImportError) as exc:
            raise SoftwareManagerError(f"Не удалось обновить реестр Windows: {exc}") from None

        broadcast_environment_change()
        return {
            "ok": True,
            "already_present": False,
            "path": target_str,
            "message": f"Каталог {target_str} успешно добавлен в переменную PATH пользователя.",
        }

    def remove_from_user_path(self, directory: Path | str) -> dict[str, Any]:
        target = Path(directory).resolve()
        target_str = str(target)
        current = self.get_user_path()
        target_norm = target_str.casefold().rstrip("\\/")

        filtered = [p for p in current if p.casefold().rstrip("\\/") != target_norm]
        if len(filtered) == len(current):
            return {
                "ok": True,
                "found": False,
                "path": target_str,
                "message": f"Каталог {target_str} не найден в переменной PATH пользователя.",
            }

        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
                _, reg_type = winreg.QueryValueEx(key, "Path")
                new_val = ";".join(filtered)
                winreg.SetValueEx(key, "Path", 0, reg_type, new_val)
        except (OSError, ImportError) as exc:
            raise SoftwareManagerError(f"Не удалось обновить реестр Windows: {exc}") from None

        broadcast_environment_change()
        return {
            "ok": True,
            "found": True,
            "path": target_str,
            "message": f"Каталог {target_str} успешно удалён из переменной PATH пользователя.",
        }

    @staticmethod
    def check_command(command_name: str) -> dict[str, Any]:
        clean_name = str(command_name or "").strip()
        if not clean_name:
            return {"command": "", "found": False, "path": None}
        found_path = shutil.which(clean_name)
        return {
            "command": clean_name,
            "found": found_path is not None,
            "path": str(found_path) if found_path else None,
        }

    @staticmethod
    def describe_action(
        package_or_file: str,
        tier: str,
        *,
        source: str = "",
        size_bytes: int | None = None,
        is_silent: bool = True,
    ) -> str:
        tier_names = {
            "winget": "пакетный менеджер winget",
            "download": "прямую загрузку официального инсталлятора",
            "interactive": "интерактивный мастер установки",
            "portable": "распаковку портативного архива",
        }
        tier_text = tier_names.get(tier, tier)
        size_text = f", размер {size_bytes // 1_048_576} МБ" if size_bytes and size_bytes > 1_048_576 else ""
        mode_text = "тихий режим (без окон)" if is_silent else "видимое окно мастера"
        source_text = f" из {source}" if source else ""
        return (
            f"Запрос на установку '{package_or_file}' через {tier_text}{source_text}{size_text}. "
            f"Режим: {mode_text}. Разрешить действие?"
        )
