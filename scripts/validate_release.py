from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any


LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
LOCK_LINE_RE = re.compile(r"^[A-Za-z0-9_.-]+==[^\s]+$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class ValidationError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"Не найден обязательный файл: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Некорректный JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"Корень JSON должен быть объектом: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _validate_required_files(root: Path, manifest: dict[str, Any]) -> None:
    required = manifest.get("required_files")
    if not isinstance(required, list) or not required:
        raise ValidationError("release-manifest.json не содержит required_files.")
    duplicates = sorted({item for item in required if required.count(item) > 1})
    if duplicates:
        raise ValidationError(f"Дубли required_files: {', '.join(duplicates)}")
    missing = [str(item) for item in required if not (root / str(item)).is_file()]
    if missing:
        raise ValidationError("Отсутствуют обязательные файлы: " + ", ".join(missing))


def _validate_versions(root: Path, manifest: dict[str, Any]) -> None:
    project = manifest.get("project")
    if not isinstance(project, dict):
        raise ValidationError("В release manifest отсутствует объект project.")
    release_version = str(project.get("version", "")).strip()
    if not release_version:
        raise ValidationError("Не задана версия релиза.")
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = str(pyproject.get("project", {}).get("version", ""))
    if package_version != release_version:
        raise ValidationError(
            f"Версии расходятся: pyproject={package_version}, release={release_version}."
        )

    runtime = _load_json(root / "config" / "runtime-assets.lock.json")
    python_version = str(runtime.get("python", {}).get("version", ""))
    if not python_version.startswith("3.12."):
        raise ValidationError(f"Неподдерживаемая закреплённая версия Python: {python_version}")
    torch_version = str(runtime.get("packages", {}).get("torch", ""))
    if "+cu" not in torch_version:
        raise ValidationError("Версия Torch должна явно содержать проверенный CUDA variant.")

    engine = _load_json(root / "config" / "engine.lock.json")
    for key in ("release", "commit", "cuda", "assets"):
        if not engine.get(key):
            raise ValidationError(f"engine.lock.json не содержит {key}.")
    assets = engine["assets"]
    if not isinstance(assets, dict) or len(assets) < 2:
        raise ValidationError("Для llama.cpp нужны binary и CUDA runtime archives.")
    for name, digest in assets.items():
        if not str(name).endswith(".zip") or not re.fullmatch(r"[A-Fa-f0-9]{64}", str(digest)):
            raise ValidationError(f"Некорректный asset/hash llama.cpp: {name}")


def _validate_requirements(root: Path, manifest: dict[str, Any]) -> None:
    locks = manifest.get("component_locks", [])
    if not isinstance(locks, list) or not locks:
        raise ValidationError("Не перечислены component_locks.")
    for item in locks:
        path = root / str(item)
        if not path.is_file():
            raise ValidationError(f"Не найден lock-файл: {item}")

    for relative in ("requirements/runtime.lock.txt", "requirements/torch-cu128.lock.txt"):
        path = root / relative
        packages: list[str] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if not LOCK_LINE_RE.fullmatch(line):
                raise ValidationError(f"Незакреплённая строка в {relative}: {line}")
            packages.append(line.casefold().split("==", 1)[0])
        if not packages:
            raise ValidationError(f"Пустой lock-файл: {relative}")
        if len(packages) != len(set(packages)):
            raise ValidationError(f"Дубли пакетов в {relative}")


def _validate_hardware_profiles(root: Path) -> None:
    value = _load_json(root / "config" / "hardware-profiles.json")
    profiles = value.get("profiles")
    if not isinstance(profiles, list) or len(profiles) < 4:
        raise ValidationError("Нужно не менее четырёх аппаратных профилей.")
    ids: set[str] = set()
    previous_vram = -1
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValidationError("Аппаратный профиль должен быть объектом.")
        profile_id = str(profile.get("id", ""))
        if not profile_id or profile_id in ids:
            raise ValidationError(f"Пустой или повторный hardware profile: {profile_id}")
        ids.add(profile_id)
        vram = int(profile.get("minimum_vram_mb", -1))
        context = int(profile.get("recommended_context", 0))
        if vram < previous_vram:
            raise ValidationError("Аппаратные профили должны быть отсортированы по VRAM.")
        if context not in {8192, 16384, 32768, 65536}:
            raise ValidationError(f"Неподдерживаемый стартовый context: {context}")
        if profile.get("cache_type_k") != "q8_0" or profile.get("cache_type_v") != "q5_0":
            raise ValidationError(f"Профиль {profile_id} потерял асимметричный KV.")
        previous_vram = vram


def _validate_portable_defaults(root: Path) -> None:
    value = _load_json(root / "config" / "default.json")
    absolute_paths: list[str] = []

    def walk(item: object, key: str = "") -> None:
        if isinstance(item, dict):
            for name, child in item.items():
                walk(child, f"{key}.{name}" if key else str(name))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{key}[{index}]")
        elif isinstance(item, str):
            candidate = item.strip()
            if WINDOWS_ABSOLUTE_RE.match(candidate) or candidate.startswith("\\\\"):
                absolute_paths.append(f"{key}={candidate}")

    walk(value)
    if absolute_paths:
        raise ValidationError(
            "default.json содержит привязанные к компьютеру пути: "
            + "; ".join(absolute_paths)
        )


def _validate_markdown_links(root: Path) -> None:
    markdown = sorted(root.glob("*.md"))
    markdown.extend(sorted((root / "docs").glob("*.md")))
    markdown.extend(sorted((root / ".github").rglob("*.md")))
    broken: list[str] = []
    for document in markdown:
        if not document.is_file():
            continue
        for target in LINK_RE.findall(document.read_text(encoding="utf-8")):
            clean = target.strip().strip("<>")
            if not clean or clean.startswith(("#", "http://", "https://", "mailto:")):
                continue
            clean = clean.split("#", 1)[0]
            if not clean or re.match(r"^[A-Za-z]:[/\\]", clean):
                continue
            candidate = (document.parent / clean).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                broken.append(f"{_relative(root, document)} -> {target}")
                continue
            if not candidate.exists():
                broken.append(f"{_relative(root, document)} -> {target}")
    if broken:
        raise ValidationError("Неработающие локальные ссылки: " + "; ".join(broken))


def _path_is_forbidden(relative: str, package: dict[str, Any]) -> bool:
    normalized = relative.replace("\\", "/").strip("/")
    lower = normalized.casefold()
    excluded_files = {str(item).casefold() for item in package.get("excluded_files", [])}
    if lower in excluded_files:
        return True
    parts = lower.split("/")
    for raw in package.get("excluded_path_parts", []):
        forbidden = str(raw).replace("\\", "/").strip("/").casefold()
        if "/" in forbidden:
            if lower == forbidden or lower.startswith(forbidden + "/"):
                return True
        elif forbidden in parts:
            return True
    suffix = Path(normalized).suffix.casefold()
    return suffix in {str(item).casefold() for item in package.get("forbidden_extensions", [])}


def _validate_package(root: Path, manifest: dict[str, Any]) -> int:
    package_rules = manifest.get("package")
    if not isinstance(package_rules, dict):
        raise ValidationError("Отсутствуют правила package.")
    forbidden = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = _relative(root, path)
        if relative == "PACKAGE-MANIFEST.json":
            continue
        if _path_is_forbidden(relative, package_rules):
            forbidden.append(relative)
    if forbidden:
        raise ValidationError("В пакете найдены запрещённые файлы: " + ", ".join(forbidden))

    package_manifest_path = root / "PACKAGE-MANIFEST.json"
    if not package_manifest_path.is_file():
        raise ValidationError("Пакет не содержит PACKAGE-MANIFEST.json.")
    package_manifest = _load_json(package_manifest_path)
    entries = package_manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValidationError("PACKAGE-MANIFEST.json не содержит файлов.")

    declared: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationError("Некорректная запись package manifest.")
        relative = str(entry.get("path", "")).replace("\\", "/")
        if not relative or relative in declared or relative == "PACKAGE-MANIFEST.json":
            raise ValidationError(f"Повторный/некорректный путь package manifest: {relative}")
        declared[relative] = entry

    actual = {
        _relative(root, path)
        for path in root.rglob("*")
        if path.is_file() and path != package_manifest_path
    }
    if actual != set(declared):
        missing = sorted(set(declared) - actual)
        extra = sorted(actual - set(declared))
        raise ValidationError(f"Состав пакета расходится: missing={missing}, extra={extra}")

    for relative in sorted(actual):
        path = root / relative
        entry = declared[relative]
        if int(entry.get("size_bytes", -1)) != path.stat().st_size:
            raise ValidationError(f"Размер не совпал: {relative}")
        if str(entry.get("sha256", "")).upper() != _sha256(path):
            raise ValidationError(f"SHA-256 не совпал: {relative}")
    return len(actual)


def validate(root: Path, *, package_mode: bool | None = None) -> dict[str, Any]:
    root = root.resolve()
    manifest = _load_json(root / "config" / "release-manifest.json")
    if int(manifest.get("schema_version", 0)) != 1:
        raise ValidationError("Неподдерживаемая версия release manifest.")
    _validate_required_files(root, manifest)
    _validate_versions(root, manifest)
    _validate_requirements(root, manifest)
    _validate_hardware_profiles(root)
    _validate_portable_defaults(root)
    _validate_markdown_links(root)

    is_package = (root / "PACKAGE-MANIFEST.json").is_file() if package_mode is None else package_mode
    file_count = _validate_package(root, manifest) if is_package else 0
    return {
        "ok": True,
        "root": str(root),
        "version": manifest["project"]["version"],
        "package": is_package,
        "package_file_count": file_count,
        "required_file_count": len(manifest["required_files"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка исходного дерева или пакета Ксении.")
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--require-package", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = validate(args.package_root, package_mode=True if args.require_package else None)
    except (OSError, ValueError, ValidationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
