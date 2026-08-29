from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from butler.config import ConfigError, load_settings  # noqa: E402
from butler.model_assets import (  # noqa: E402
    ModelAssetError,
    download_model_asset,
    model_asset_from_config,
    model_assets_from_config,
    verify_model_asset,
    verify_model_path,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Загрузка и полная проверка закреплённых GGUF-моделей Ксении"
    )
    parser.add_argument(
        "action",
        choices=("verify", "download"),
        help="verify проверяет существующий файл; download загружает или проверяет его",
    )
    parser.add_argument("profile", help="идентификатор профиля из config/default.json")
    parser.add_argument(
        "--asset",
        help="проверить или загрузить один артефакт; по умолчанию обрабатываются все",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        help="явный каталог моделей; по умолчанию используется paths.models_dir",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    try:
        settings = load_settings(ROOT)
        models = settings.raw.get("models", {})
        assets = (
            (model_asset_from_config(models, args.profile, args.asset),)
            if args.asset
            else model_assets_from_config(models, args.profile)
        )
        models_dir = (args.models_dir or settings.models_dir).expanduser().resolve()
        if args.action == "download":
            reports = [download_model_asset(asset, models_dir) for asset in assets]
        else:
            if args.models_dir:
                reports = [
                    verify_model_asset(asset, models_dir, verify_hash=True)
                    for asset in assets
                ]
            else:
                profile = settings.model(args.profile)
                paths = {
                    "model": profile.model_path,
                    "draft": profile.draft_model_path,
                    "projector": profile.projector_path,
                }
                reports = []
                for asset in assets:
                    path = paths.get(asset.asset)
                    if path is None:
                        raise ModelAssetError(
                            f"Для артефакта {asset.asset} не разрешён локальный путь."
                        )
                    reports.append(
                        verify_model_path(asset, path, verify_hash=True)
                    )
    except (ConfigError, ModelAssetError, OSError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 2

    report: object = reports[0] if len(reports) == 1 else reports
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
