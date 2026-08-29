from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from huggingface_hub import hf_hub_download


_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"
)
_REVISION_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ModelAssetError(RuntimeError):
    pass


def _safe_filename(value: object, *, field: str) -> str:
    filename = str(value or "").strip()
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or not filename.casefold().endswith(".gguf")
    ):
        raise ModelAssetError(f"Некорректное поле {field}: ожидается одно имя GGUF-файла.")
    return filename


@dataclass(frozen=True)
class ModelAsset:
    profile: str
    asset: str
    repo: str
    revision: str
    source_filename: str
    local_filename: str
    expected_size_bytes: int
    sha256: str

    @classmethod
    def from_mapping(
        cls,
        profile: str,
        value: Mapping[str, Any],
        *,
        asset: str = "model",
    ) -> "ModelAsset":
        clean_profile = str(profile).strip()
        if not _PROFILE_RE.fullmatch(clean_profile):
            raise ModelAssetError("Некорректный идентификатор профиля модели.")
        clean_asset = str(asset).strip()
        if not _PROFILE_RE.fullmatch(clean_asset):
            raise ModelAssetError("Некорректный идентификатор артефакта модели.")

        repo = str(value.get("source_repo", "")).strip()
        if not _REPOSITORY_RE.fullmatch(repo):
            raise ModelAssetError(
                f"Профиль {clean_profile} не содержит корректный source_repo."
            )

        revision = str(value.get("source_revision", "")).strip().casefold()
        if not _REVISION_RE.fullmatch(revision):
            raise ModelAssetError(
                f"Профиль {clean_profile} должен закреплять полный 40-символьный commit."
            )

        source_filename = _safe_filename(
            value.get("source_filename"), field="source_filename"
        )
        local_filename = _safe_filename(value.get("filename"), field="filename")
        if source_filename != local_filename:
            raise ModelAssetError(
                f"Профиль {clean_profile} пока не поддерживает переименование GGUF."
            )

        try:
            expected_size_bytes = int(value.get("expected_size_bytes", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ModelAssetError("Некорректный ожидаемый размер модели.") from exc
        if expected_size_bytes <= 0:
            raise ModelAssetError(
                f"Профиль {clean_profile} не содержит положительный expected_size_bytes."
            )

        sha256 = str(value.get("sha256", "")).strip().casefold()
        if not _SHA256_RE.fullmatch(sha256):
            raise ModelAssetError(
                f"Профиль {clean_profile} не содержит полный SHA-256."
            )

        return cls(
            profile=clean_profile,
            asset=clean_asset,
            repo=repo,
            revision=revision,
            source_filename=source_filename,
            local_filename=local_filename,
            expected_size_bytes=expected_size_bytes,
            sha256=sha256,
        )


def model_asset_from_config(
    models: Mapping[str, Any], profile: str, asset: str = "model"
) -> ModelAsset:
    if not isinstance(models, Mapping):
        raise ModelAssetError("Раздел models в конфигурации повреждён.")
    value = models.get(profile)
    if not isinstance(value, Mapping):
        raise ModelAssetError(f"Неизвестный профиль модели: {profile}")
    artifacts = value.get("artifacts")
    if isinstance(artifacts, Mapping):
        artifact_value = artifacts.get(asset)
        if not isinstance(artifact_value, Mapping):
            raise ModelAssetError(
                f"Профиль {profile} не содержит артефакт {asset}."
            )
        return ModelAsset.from_mapping(
            profile, artifact_value, asset=asset
        )
    if asset != "model":
        raise ModelAssetError(
            f"Устаревший профиль {profile} не содержит артефакт {asset}."
        )
    return ModelAsset.from_mapping(profile, value, asset=asset)


def model_assets_from_config(
    models: Mapping[str, Any], profile: str
) -> tuple[ModelAsset, ...]:
    if not isinstance(models, Mapping):
        raise ModelAssetError("Раздел models в конфигурации повреждён.")
    value = models.get(profile)
    if not isinstance(value, Mapping):
        raise ModelAssetError(f"Неизвестный профиль модели: {profile}")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return (model_asset_from_config(models, profile),)
    result = []
    for name, artifact_value in artifacts.items():
        if not isinstance(artifact_value, Mapping):
            raise ModelAssetError(
                f"Артефакт {name} профиля {profile} повреждён."
            )
        result.append(
            ModelAsset.from_mapping(profile, artifact_value, asset=str(name))
        )
    if not result or all(item.asset != "model" for item in result):
        raise ModelAssetError(
            f"Профиль {profile} не содержит основной артефакт model."
        )
    return tuple(result)


def _target_path(asset: ModelAsset, models_dir: Path) -> tuple[Path, Path]:
    root = models_dir.expanduser().resolve()
    target = root / asset.local_filename
    return root, target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_asset(
    asset: ModelAsset,
    models_dir: Path,
    *,
    verify_hash: bool = True,
) -> dict[str, object]:
    root, target = _target_path(asset, models_dir)
    return verify_model_path(asset, target, required_parent=root, verify_hash=verify_hash)


def verify_model_path(
    asset: ModelAsset,
    target: Path,
    *,
    required_parent: Path | None = None,
    verify_hash: bool = True,
) -> dict[str, object]:
    target = target.expanduser()
    if target.is_symlink():
        raise ModelAssetError(f"Символическая ссылка модели запрещена: {target}")
    target = target.resolve()
    if not target.is_file():
        raise ModelAssetError(f"Файл модели не найден: {target}")

    if required_parent is not None and target.parent != required_parent.resolve():
        raise ModelAssetError("Файл модели разрешается за пределы каталога моделей.")

    actual_size = target.stat().st_size
    if actual_size != asset.expected_size_bytes:
        raise ModelAssetError(
            f"Неверный размер {target.name}: {actual_size}; "
            f"ожидалось {asset.expected_size_bytes}."
        )

    actual_hash = _sha256(target) if verify_hash else ""
    if verify_hash and actual_hash != asset.sha256:
        raise ModelAssetError(
            f"SHA-256 {target.name} не совпал с закреплённым значением."
        )

    return {
        "profile": asset.profile,
        "asset": asset.asset,
        "path": str(target),
        "repo": asset.repo,
        "revision": asset.revision,
        "size_bytes": actual_size,
        "size_matches": True,
        "sha256": actual_hash if verify_hash else None,
        "sha256_matches": True if verify_hash else None,
    }


def download_model_asset(
    asset: ModelAsset,
    models_dir: Path,
) -> dict[str, object]:
    root, target = _target_path(asset, models_dir)
    root.mkdir(parents=True, exist_ok=True)

    if target.exists():
        return verify_model_asset(asset, root, verify_hash=True)

    try:
        downloaded = Path(
            hf_hub_download(
                repo_id=asset.repo,
                filename=asset.source_filename,
                revision=asset.revision,
                local_dir=str(root),
                token=False,
            )
        )
    except Exception as exc:
        raise ModelAssetError(
            f"Не удалось загрузить закреплённый файл {asset.source_filename} "
            f"({type(exc).__name__})."
        ) from None
    if downloaded.resolve() != target.resolve():
        raise ModelAssetError(
            "Hugging Face вернул неожиданный путь; файл не допускается к использованию."
        )
    return verify_model_asset(asset, root, verify_hash=True)
