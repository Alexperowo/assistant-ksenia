from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from butler.config import Settings


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class Authorization:
    decision: Decision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


class PermissionBroker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        policy = settings.raw.get("permissions", {})
        self.actions = {
            str(name): Decision(str(value))
            for name, value in policy.get("actions", {}).items()
        }
        roots: list[Path] = []
        for raw in policy.get("allowed_roots", []):
            if raw == "workspace":
                roots.append(settings.root)
            elif raw == "projects":
                workspace = settings.raw.get("developer", {}).get("workspace_dir", "workspace")
                workspace_path = Path(str(workspace))
                roots.append(
                    workspace_path.resolve()
                    if workspace_path.is_absolute()
                    else (settings.root / workspace_path).resolve()
                )
            else:
                roots.append(Path(str(raw)).resolve())
        self.allowed_roots = tuple(roots)
        self.protected_paths = (
            (settings.root / ".git").resolve(),
            (settings.root / "config" / "default.json").resolve(),
            (settings.root / "config" / "user.json").resolve(),
            (settings.runtime_dir / "undo").resolve(),
        )

    def _inside_allowed_root(self, target: Path) -> bool:
        resolved = target.resolve()
        return any(resolved == root or root in resolved.parents for root in self.allowed_roots)

    def authorize(
        self, action: str, target: Path | None = None, confirmed: bool = False
    ) -> Authorization:
        decision = self.actions.get(action, Decision.DENY)
        if target is not None and not self._inside_allowed_root(target):
            return Authorization(Decision.DENY, "Цель находится вне разрешённых каталогов.")
        if target is not None and action in {"write_file", "delete_file", "run_command"}:
            resolved = target.resolve()
            if any(resolved == item or item in resolved.parents for item in self.protected_paths):
                return Authorization(Decision.DENY, "Защищённый системный файл Ксении нельзя изменять агентом.")
        if decision == Decision.DENY:
            return Authorization(Decision.DENY, "Действие запрещено политикой.")
        if decision == Decision.CONFIRM and not confirmed:
            return Authorization(Decision.CONFIRM, "Требуется подтверждение пользователя.")
        return Authorization(Decision.ALLOW, "Разрешено.")
