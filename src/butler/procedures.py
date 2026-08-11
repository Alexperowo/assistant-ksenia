from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class ProcedureError(RuntimeError):
    pass


class ProcedureLibrary:
    """Read-only, auditable workflows that can be extended without core changes."""

    def __init__(self, root: Path) -> None:
        self.root = root / "procedures"

    @staticmethod
    def _valid_name(name: object) -> str:
        value = str(name or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            raise ProcedureError("Недопустимое имя процедуры.")
        return value

    def list(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        if not self.root.is_dir():
            return result
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.name):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            result.append(
                {
                    "name": path.stem,
                    "title": str(value.get("title", path.stem)),
                    "purpose": str(value.get("purpose", "")),
                }
            )
        return result

    def read(self, name: object) -> dict[str, Any]:
        safe_name = self._valid_name(name)
        path = self.root / f"{safe_name}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProcedureError(f"Процедура не найдена: {safe_name}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcedureError(f"Процедура повреждена: {safe_name}") from exc
        if not isinstance(value, dict):
            raise ProcedureError(f"Процедура имеет неверный формат: {safe_name}")
        steps = value.get("steps", [])
        if not isinstance(steps, list) or not all(isinstance(item, str) for item in steps):
            raise ProcedureError(f"Шаги процедуры имеют неверный формат: {safe_name}")
        return {"name": safe_name, **value}
