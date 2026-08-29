from __future__ import annotations

from pathlib import Path

from butler.config import Settings


def find_models(settings: Settings, limit: int = 200) -> list[Path]:
    configured = settings.raw.get("paths", {}).get("model_search_dirs", [])
    roots = [
        settings.models_dir,
        *(
            path if path.is_absolute() else settings.root / path
            for path in (Path(str(item)).expanduser() for item in configured)
        ),
    ]
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if not resolved_root.is_dir():
            continue
        try:
            for path in resolved_root.rglob("*.gguf"):
                resolved = path.resolve()
                if resolved in seen or not resolved.is_file():
                    continue
                seen.add(resolved)
                result.append(resolved)
                if len(result) >= limit:
                    break
        except OSError:
            continue
        if len(result) >= limit:
            break
    return sorted(result, key=lambda item: item.name.casefold())
