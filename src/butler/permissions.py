from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from butler.config import Settings


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


_RESTRICTION = {
    Decision.ALLOW: 0,
    Decision.CONFIRM: 1,
    Decision.DENY: 2,
}

# Personal configuration may make a rule stricter, but it must never remove
# these product-level safeguards. They are part of Ksenia's security contract,
# not convenience defaults.
SAFETY_MINIMUM = {
    "write_file": Decision.CONFIRM,
    "run_tests": Decision.CONFIRM,
    "run_command": Decision.CONFIRM,
    "windows_write": Decision.CONFIRM,
    "browser_write": Decision.CONFIRM,
    "memory_write": Decision.CONFIRM,
    "memory_delete": Decision.CONFIRM,
    "delete_file": Decision.CONFIRM,
    "install_software": Decision.CONFIRM,
    "send_message": Decision.CONFIRM,
    "financial_action": Decision.DENY,
}


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
        self.actions: dict[str, Decision] = dict(SAFETY_MINIMUM)
        for name, value in policy.get("actions", {}).items():
            action = str(name)
            decision = Decision(str(value))
            minimum = SAFETY_MINIMUM.get(action)
            if minimum is not None and _RESTRICTION[decision] < _RESTRICTION[minimum]:
                decision = minimum
            self.actions[action] = decision

        raw_workspace = Path(
            str(settings.raw.get("developer", {}).get("workspace_dir", "."))
        )
        workspace_root = (
            raw_workspace.resolve()
            if raw_workspace.is_absolute()
            else (settings.root / raw_workspace).resolve()
        )
        roots: list[Path] = []
        for raw in policy.get("allowed_roots", []):
            if raw in {"workspace", "projects"}:
                candidate = workspace_root
            else:
                configured = Path(str(raw))
                candidate = (
                    configured.resolve()
                    if configured.is_absolute()
                    else (settings.root / configured).resolve()
                )
            if candidate == workspace_root or workspace_root in candidate.parents:
                roots.append(candidate)
        self.allowed_roots = tuple(dict.fromkeys(roots))
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
