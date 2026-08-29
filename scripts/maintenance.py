from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from butler.config import load_settings  # noqa: E402
from butler.model_manager import ModelManager  # noqa: E402


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Ожидался JSON object: {path}")
    return value


def _requirements(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise RuntimeError(f"Незакреплённая строка: {path}: {line}")
        name, version = line.split("==", 1)
        result[name] = version
    return result


def engine_version_matches(text: str, build: str, commit: str) -> bool:
    escaped_build = re.escape(build)
    escaped_commit = re.escape(commit)
    legacy = rf"version:\s*{escaped_build}\s+\({escaped_commit}\)"
    current = (
        rf"version:\s*\S+\s+"
        rf"\(build\s+{escaped_build},\s+commit\s+{escaped_commit}\)"
    )
    return re.search(legacy, text) is not None or re.search(current, text) is not None


def _version_output(server: Path, root: Path) -> str:
    if not server.is_file():
        return ""
    try:
        completed = subprocess.run(
            [str(server), "--version"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=False,
            check=False,
        )
        return (completed.stdout + completed.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _locked_backend_status(
    root: Path,
    settings: Any,
    name: str,
    item: dict[str, Any],
) -> dict[str, Any]:
    configured = settings.engine_backend(name)
    relative = Path(str(item.get("executable", "")))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Некорректный executable backend-а {name} в engine lock")
    expected_path = (root / relative).resolve()
    server = configured.executable.resolve()
    version_text = _version_output(server, root)
    build = str(item.get("version_build", ""))
    commit = str(item.get("version_commit", ""))
    version_matches = bool(
        build
        and commit
        and engine_version_matches(version_text.replace("\r", ""), build, commit)
    )
    runtime_files = item.get("runtime_files", {})
    if not isinstance(runtime_files, dict):
        raise RuntimeError(f"runtime_files backend-а {name} должен быть объектом")
    files_match = True
    checked_files = 0
    for filename, expected in runtime_files.items():
        if Path(filename).name != filename or not isinstance(expected, dict):
            raise RuntimeError(f"Некорректный runtime-файл backend-а {name}: {filename}")
        path = server.parent / filename
        try:
            size_matches = path.stat().st_size == int(expected["size"])
            hash_matches = _sha256_file(path) == str(expected["sha256"]).casefold()
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError):
            size_matches = False
            hash_matches = False
        checked_files += 1
        files_match = files_match and size_matches and hash_matches
    patch = item.get("patch")
    patch_matches = True
    if patch is not None:
        if not isinstance(patch, dict):
            raise RuntimeError(f"Patch backend-а {name} повреждён")
        patch_relative = Path(str(patch.get("path", "")))
        patch_root = (root / "config" / "patches").resolve()
        patch_path = (root / patch_relative).resolve()
        try:
            patch_path.relative_to(patch_root)
            patch_matches = (
                patch_path.is_file()
                and _sha256_file(patch_path) == str(patch.get("sha256", "")).casefold()
            )
        except (ValueError, OSError):
            patch_matches = False
    path_matches = server == expected_path
    return {
        "name": name,
        "distribution": str(item.get("distribution", "")),
        "expected_release": item.get("release"),
        "expected_commit": str(item.get("commit", "")),
        "path": str(server),
        "expected_path": str(expected_path),
        "version_output": version_text,
        "checked_runtime_files": checked_files,
        "version_matches": version_matches,
        "runtime_files_match": files_match,
        "patch_matches": patch_matches,
        "path_matches": path_matches,
        "matches": bool(
            server.is_file()
            and path_matches
            and version_matches
            and files_match
            and patch_matches
        ),
    }


def component_status(root: Path) -> dict[str, Any]:
    root = root.resolve()
    runtime_lock = _json(root / "config" / "runtime-assets.lock.json")
    engine_lock = _json(root / "config" / "engine.lock.json")
    expected_packages = {"pip": str(runtime_lock["python"]["pip"])}
    torch_relative = Path(str(runtime_lock["torch"]["requirements"]))
    if torch_relative.is_absolute() or ".." in torch_relative.parts:
        raise RuntimeError("Некорректный путь Torch lock в runtime-assets.lock.json")
    torch_lock = (root / torch_relative).resolve()
    try:
        torch_lock.relative_to(root / "requirements")
    except ValueError as error:
        raise RuntimeError("Torch lock находится вне каталога requirements") from error
    expected_packages.update(_requirements(torch_lock))
    expected_packages.update(_requirements(root / "requirements" / "runtime.lock.txt"))

    package_rows: list[dict[str, Any]] = []
    for name in sorted(expected_packages, key=str.casefold):
        expected = expected_packages[name]
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        package_rows.append(
            {
                "name": name,
                "expected": expected,
                "actual": actual,
                "matches": actual == expected,
            }
        )

    settings = load_settings(root)
    locked_backends = engine_lock.get("backends")
    if isinstance(locked_backends, dict):
        backend_rows = [
            _locked_backend_status(root, settings, name, item)
            for name, item in locked_backends.items()
            if isinstance(item, dict)
        ]
        locked_names = {row["name"] for row in backend_rows}
        configured_names = set(settings.engine_backend_names())
        if locked_names != configured_names:
            raise RuntimeError(
                "Состав engine_backends расходится между default config и engine lock"
            )
        release_backends = [
            row for row in backend_rows if row["distribution"] == "official-release"
        ]
        if len(release_backends) != 1:
            raise RuntimeError(
                "Engine lock должен содержать ровно один official-release backend"
            )
        engine_row = dict(release_backends[0])
    else:
        server = settings.llama_server
        release = str(engine_lock["release"])
        build = release.lstrip("bB")
        commit = str(engine_lock["commit"])
        version_text = _version_output(server, root)
        matches = server.is_file() and engine_version_matches(
            version_text.replace("\r", ""), build, commit
        )
        engine_row = {
            "expected_release": release,
            "expected_commit": commit,
            "path": str(server),
            "version_output": version_text,
            "matches": matches,
        }
        backend_rows = [{"name": "default", **engine_row}]
    engine_matches = bool(engine_row["matches"])
    all_backends_match = all(bool(row["matches"]) for row in backend_rows)

    manager = ModelManager(settings)
    running = manager.running_state()
    voice_state = root / "runtime" / "voice" / "agent.json"
    return {
        "ok": True,
        "python": {
            "expected": str(runtime_lock["python"]["version"]),
            "actual": ".".join(str(item) for item in sys.version_info[:3]),
            "executable": sys.executable,
            "matches": ".".join(str(item) for item in sys.version_info[:3])
            == str(runtime_lock["python"]["version"]),
        },
        "packages": package_rows,
        "packages_match": all(row["matches"] for row in package_rows),
        "engine": engine_row,
        "engine_backends": backend_rows,
        "all_engine_backends_match": all_backends_match,
        "model": asdict(running) if running else None,
        "voice_state_present": voice_state.is_file(),
        "all_components_match": bool(
            engine_matches
            and all_backends_match
            and all(row["matches"] for row in package_rows)
            and ".".join(str(item) for item in sys.version_info[:3])
            == str(runtime_lock["python"]["version"])
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Безопасное обслуживание Ксении.")
    parser.add_argument("command", choices=("status", "stop-model", "start-model"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--role")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = component_status(args.root)
        else:
            settings = load_settings(args.root)
            manager = ModelManager(settings)
            if args.command == "stop-model":
                before = manager.running_state()
                stopped = manager.stop()
                result = {
                    "ok": True,
                    "stopped": stopped,
                    "previous": asdict(before) if before else None,
                }
            else:
                if not args.role:
                    raise RuntimeError("Для start-model требуется --role.")
                result = {"ok": True, "started": asdict(manager.start(args.role))}
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
