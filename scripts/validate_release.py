from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
import tempfile
import tomllib
import unicodedata
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any


LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
LOCK_LINE_RE = re.compile(r"^[A-Za-z0-9_.-]+==[^\s]+$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
ARCHIVE_SHA_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
MAX_ARCHIVE_BYTES = 1_073_741_824
MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 268_435_456
MAX_ARCHIVE_EXPANDED_BYTES = 1_073_741_824
MAX_ARCHIVE_PATH_CHARS = 240
MAX_COMPRESSION_RATIO = 250
WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "con",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
    "nul",
    "prn",
}


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


def _safe_archive_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    raw = info.filename
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValidationError(f"Неоднозначный путь ZIP: {raw!r}")
    trimmed = raw[:-1] if raw.endswith("/") else raw
    if not trimmed or trimmed.startswith("/") or len(trimmed) > MAX_ARCHIVE_PATH_CHARS:
        raise ValidationError(f"Небезопасный путь ZIP: {raw!r}")
    parts = tuple(trimmed.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(f"Traversal или пустой сегмент ZIP: {raw!r}")
    if PurePosixPath(trimmed).is_absolute() or WINDOWS_ABSOLUTE_RE.match(trimmed):
        raise ValidationError(f"Абсолютный путь ZIP запрещён: {raw!r}")
    for part in parts:
        if part.endswith((" ", ".")) or ":" in part:
            raise ValidationError(f"Небезопасный Windows-путь ZIP: {raw!r}")
        device = part.split(".", 1)[0].casefold()
        if device in WINDOWS_RESERVED_NAMES:
            raise ValidationError(f"Зарезервированное имя Windows в ZIP: {raw!r}")
    return parts


def _inspect_archive(
    archive: zipfile.ZipFile,
) -> tuple[list[tuple[zipfile.ZipInfo, tuple[str, ...]]], tuple[str, ...]]:
    inspected: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    seen: set[str] = set()
    manifests: list[tuple[str, ...]] = []
    expanded_bytes = 0
    file_count = 0
    for info in archive.infolist():
        parts = _safe_archive_parts(info)
        normalized = unicodedata.normalize("NFC", "/".join(parts)).casefold()
        if normalized in seen:
            raise ValidationError(f"Повторный Windows-путь в ZIP: {info.filename}")
        seen.add(normalized)
        if info.flag_bits & 0x1:
            raise ValidationError(f"Зашифрованный элемент ZIP запрещён: {info.filename}")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValidationError(f"Неподдерживаемое сжатие ZIP: {info.filename}")
        unix_mode = info.external_attr >> 16
        if info.create_system == 3 and unix_mode:
            if stat.S_ISLNK(unix_mode):
                raise ValidationError(f"Символическая ссылка в ZIP запрещена: {info.filename}")
            file_type = stat.S_IFMT(unix_mode)
            if file_type and not (stat.S_ISREG(unix_mode) or stat.S_ISDIR(unix_mode)):
                raise ValidationError(f"Специальный файл в ZIP запрещён: {info.filename}")
        if info.is_dir():
            inspected.append((info, parts))
            continue
        file_count += 1
        if file_count > MAX_ARCHIVE_FILES:
            raise ValidationError("ZIP содержит слишком много файлов.")
        if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValidationError(f"Слишком большой элемент ZIP: {info.filename}")
        expanded_bytes += info.file_size
        if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValidationError("Суммарный распакованный размер ZIP превышает предел.")
        if info.file_size and info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO:
            raise ValidationError(f"Подозрительная степень сжатия ZIP: {info.filename}")
        if parts[-1] == "PACKAGE-MANIFEST.json":
            manifests.append(parts)
        inspected.append((info, parts))
    if file_count == 0:
        raise ValidationError("ZIP не содержит файлов.")
    if len(manifests) != 1:
        raise ValidationError(
            f"Ожидался один PACKAGE-MANIFEST.json, найдено: {len(manifests)}"
        )
    package_prefix = manifests[0][:-1]
    for info, parts in inspected:
        if info.is_dir():
            continue
        if parts[: len(package_prefix)] != package_prefix:
            raise ValidationError(
                f"Файл находится вне корня пакета: {info.filename}"
            )
    return inspected, package_prefix


def _extract_inspected_archive(
    archive: zipfile.ZipFile,
    inspected: list[tuple[zipfile.ZipInfo, tuple[str, ...]]],
    destination: Path,
) -> None:
    if destination.exists():
        raise ValidationError(f"Каталог распаковки уже существует: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        for info, parts in inspected:
            target = destination.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with archive.open(info, "r") as source, target.open("xb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > info.file_size or written > MAX_ARCHIVE_MEMBER_BYTES:
                        raise ValidationError(
                            f"Распакованный размер не совпал: {info.filename}"
                        )
                    output.write(chunk)
            if written != info.file_size:
                raise ValidationError(f"Распакованный размер не совпал: {info.filename}")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


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
    build_requires = pyproject.get("build-system", {}).get("requires", [])
    if not isinstance(build_requires, list):
        raise ValidationError("build-system.requires должен быть списком exact requirements.")
    setuptools_requirements = [
        str(item) for item in build_requires if str(item).casefold().startswith("setuptools")
    ]
    if len(setuptools_requirements) != 1 or not LOCK_LINE_RE.fullmatch(
        setuptools_requirements[0]
    ):
        raise ValidationError("setuptools в pyproject должен быть закреплён через ==.")

    runtime = _load_json(root / "config" / "runtime-assets.lock.json")
    python_version = str(runtime.get("python", {}).get("version", ""))
    if not python_version.startswith("3.12."):
        raise ValidationError(f"Неподдерживаемая закреплённая версия Python: {python_version}")
    torch_version = str(runtime.get("packages", {}).get("torch", ""))
    if "+cu" not in torch_version:
        raise ValidationError("Версия Torch должна явно содержать проверенный CUDA variant.")
    torch_config = runtime.get("torch")
    if not isinstance(torch_config, dict):
        raise ValidationError("runtime-assets.lock.json не содержит объект torch.")
    torch_requirements = str(torch_config.get("requirements", "")).replace("\\", "/")
    torch_index = str(torch_config.get("index_url", "")).rstrip("/")
    if not re.fullmatch(r"requirements/[A-Za-z0-9_.-]+\.txt", torch_requirements):
        raise ValidationError("Некорректный относительный путь Torch lock.")
    if not re.fullmatch(r"https://download\.pytorch\.org/whl/(?:cu\d+|cpu)", torch_index):
        raise ValidationError("Torch должен использовать официальный HTTPS index PyTorch.")
    torch_variant = torch_version.split("+", 1)[1]
    if not torch_index.endswith("/" + torch_variant):
        raise ValidationError("CUDA variant Torch не совпадает с настроенным index URL.")
    torch_lines = {
        line.strip()
        for line in (root / torch_requirements).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if torch_lines != {f"torch=={torch_version}"}:
        raise ValidationError("Torch lock не совпадает с runtime-assets.lock.json.")
    runtime_lines = {
        line.strip()
        for line in (root / "requirements" / "runtime.lock.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if setuptools_requirements[0] not in runtime_lines:
        raise ValidationError("Версия setuptools расходится между pyproject и runtime lock.")

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
    requirement_locks: list[str] = []
    for item in locks:
        relative = str(item).replace("\\", "/")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or WINDOWS_ABSOLUTE_RE.match(relative):
            raise ValidationError(f"Некорректный путь component lock: {item}")
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValidationError(f"Component lock выходит за корень: {item}") from error
        if not path.is_file():
            raise ValidationError(f"Не найден lock-файл: {item}")
        if relative.startswith("requirements/"):
            requirement_locks.append(relative)

    if not requirement_locks:
        raise ValidationError("В component_locks не перечислены Python requirements.")
    for relative in requirement_locks:
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


def validate_archive(
    archive_path: Path,
    extraction_root: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate and extract a ZIP strictly as data using this trusted module."""

    archive_path = archive_path.resolve()
    extraction_root = extraction_root.resolve()
    if not archive_path.is_file():
        raise ValidationError(f"Архив не найден: {archive_path}")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValidationError("ZIP превышает допустимый размер.")
    archive_sha256 = _sha256(archive_path)
    expected = str(expected_sha256 or "").strip().upper()
    if expected:
        if not ARCHIVE_SHA_RE.fullmatch(expected):
            raise ValidationError("Ожидаемый SHA-256 должен содержать 64 hex-символа.")
        if archive_sha256 != expected:
            raise ValidationError("SHA-256 ZIP не совпадает с доверенным значением.")

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            inspected, package_prefix = _inspect_archive(archive)
            _extract_inspected_archive(archive, inspected, extraction_root)
    except zipfile.BadZipFile as exc:
        if extraction_root.exists():
            shutil.rmtree(extraction_root, ignore_errors=True)
        raise ValidationError(f"Повреждённый ZIP: {exc}") from exc

    try:
        package_root = extraction_root.joinpath(*package_prefix)
        report = validate(package_root, package_mode=True)
    except Exception:
        shutil.rmtree(extraction_root, ignore_errors=True)
        raise
    return {
        **report,
        "archive": str(archive_path),
        "archive_sha256": archive_sha256,
        "archive_authenticated": bool(expected),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка исходного дерева или пакета Ксении.")
    parser.add_argument("--package-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--require-package", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--extract-to", type=Path)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args(argv)
    try:
        if args.archive is not None:
            if args.extract_to is not None:
                report = validate_archive(
                    args.archive,
                    args.extract_to,
                    expected_sha256=args.expected_sha256,
                )
            else:
                with tempfile.TemporaryDirectory(prefix="ksenia-verify-") as directory:
                    report = validate_archive(
                        args.archive,
                        Path(directory) / "package",
                        expected_sha256=args.expected_sha256,
                    )
        else:
            if args.extract_to is not None or args.expected_sha256:
                raise ValidationError(
                    "--extract-to и --expected-sha256 допустимы только вместе с --archive."
                )
            report = validate(
                args.package_root,
                package_mode=True if args.require_package else None,
            )
    except (OSError, ValueError, ValidationError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
