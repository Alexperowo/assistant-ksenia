from __future__ import annotations

import argparse
import importlib.metadata
import json
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
    server = settings.llama_server
    version_text = ""
    if server.is_file():
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
            version_text = (completed.stdout + completed.stderr).strip()
        except (OSError, subprocess.TimeoutExpired):
            version_text = ""
    release = str(engine_lock["release"])
    build = release.lstrip("bB")
    commit = str(engine_lock["commit"])
    engine_matches = (
        server.is_file()
        and f"version: {build} ({commit})" in version_text.replace("\r", "")
    )

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
        "engine": {
            "expected_release": release,
            "expected_commit": commit,
            "path": str(server),
            "version_output": version_text,
            "matches": engine_matches,
        },
        "model": asdict(running) if running else None,
        "voice_state_present": voice_state.is_file(),
        "all_components_match": bool(
            engine_matches
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
