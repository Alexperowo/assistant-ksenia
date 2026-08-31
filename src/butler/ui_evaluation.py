from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from butler.ui_deliberation import ActionProposal
from butler.ui_mate import (
    UI_MATE_ACTIONS,
    UI_MATE_COORDINATE_ACTIONS,
    UI_MATE_KEY_ACTIONS,
)


class UIEvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class UIExpectation:
    action: str
    coordinate_region: tuple[int, int, int, int] | None
    keys: tuple[str, ...] | None


@dataclass(frozen=True)
class UICase:
    case_id: str
    task: str
    screenshot: Path
    expectations: tuple[UIExpectation, ...]
    forbidden_actions: tuple[str, ...]


@dataclass(frozen=True)
class UIEvaluationResult:
    case_id: str
    passed: bool
    action: str
    reasons: tuple[str, ...]


def _string_list(
    value: object,
    *,
    field: str,
    required: bool = False,
) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise UIEvaluationError(f"{field} должен быть списком непустых строк.")
    normalized = tuple(item.strip().casefold() for item in value)
    if required and not normalized:
        raise UIEvaluationError(f"{field} не должен быть пустым.")
    if len(set(normalized)) != len(normalized):
        raise UIEvaluationError(f"{field} содержит повторяющиеся значения.")
    return normalized


def _action_list(value: object, *, field: str, required: bool = False) -> tuple[str, ...]:
    actions = _string_list(value, field=field, required=required)
    unknown = sorted(set(actions) - UI_MATE_ACTIONS)
    if unknown:
        raise UIEvaluationError(f"{field} содержит неизвестные actions: {', '.join(unknown)}")
    return actions


def _coordinate_region(value: object, *, field: str) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise UIEvaluationError(f"{field} должен быть четырьмя целыми координатами.")
    left, top, right, bottom = value
    if not all(0 <= item <= 999 for item in value) or left > right or top > bottom:
        raise UIEvaluationError(f"{field} вышел за диапазон 0–999 или имеет обратные границы.")
    return left, top, right, bottom


def _safe_screenshot(root: Path, value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise UIEvaluationError(f"{field} должен содержать относительный путь.")
    relative = Path(value.strip())
    if relative.is_absolute():
        raise UIEvaluationError(f"{field} не должен быть абсолютным путём.")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UIEvaluationError(f"{field} выходит за каталог corpus.") from exc
    if resolved.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise UIEvaluationError(f"{field} имеет неподдерживаемое расширение.")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise UIEvaluationError(f"Снимок corpus не найден: {resolved.name}.") from exc
    if not 1 <= size <= 25_000_000:
        raise UIEvaluationError(f"Снимок corpus пуст или превышает 25 МБ: {resolved.name}.")
    return resolved


def _expectations(value: object, *, field: str) -> tuple[UIExpectation, ...]:
    if not isinstance(value, list) or not value:
        raise UIEvaluationError(f"{field} должен быть непустым списком.")
    expectations: list[UIExpectation] = []
    for index, raw in enumerate(value):
        item_field = f"{field}[{index}]"
        if not isinstance(raw, Mapping):
            raise UIEvaluationError(f"{item_field} должен быть объектом.")
        unknown = sorted(set(raw) - {"action", "coordinate_region", "keys"})
        if unknown:
            raise UIEvaluationError(
                f"{item_field} содержит неизвестные поля: {', '.join(unknown)}."
            )
        action = raw.get("action")
        if not isinstance(action, str) or action.strip().casefold() not in UI_MATE_ACTIONS:
            raise UIEvaluationError(f"{item_field}.action неизвестен.")
        normalized_action = action.strip().casefold()
        coordinate_region = _coordinate_region(
            raw.get("coordinate_region"), field=f"{item_field}.coordinate_region"
        )
        keys = (
            _string_list(raw.get("keys"), field=f"{item_field}.keys", required=True)
            if raw.get("keys") is not None
            else None
        )
        if normalized_action not in UI_MATE_COORDINATE_ACTIONS | UI_MATE_KEY_ACTIONS:
            raise UIEvaluationError(
                f"{item_field}.action={normalized_action} пока не имеет точного corpus-контракта."
            )
        if normalized_action in UI_MATE_COORDINATE_ACTIONS:
            if coordinate_region is None:
                raise UIEvaluationError(
                    f"{item_field}.coordinate_region обязателен для {normalized_action}."
                )
            if keys is not None:
                raise UIEvaluationError(
                    f"{item_field}.keys не применим к {normalized_action}."
                )
        if normalized_action in UI_MATE_KEY_ACTIONS:
            if keys is None:
                raise UIEvaluationError(f"{item_field}.keys обязателен для {normalized_action}.")
            if coordinate_region is not None:
                raise UIEvaluationError(
                    f"{item_field}.coordinate_region не применим к {normalized_action}."
                )
        expectation = UIExpectation(normalized_action, coordinate_region, keys)
        if expectation in expectations:
            raise UIEvaluationError(f"{field} содержит повторяющийся вариант.")
        expectations.append(expectation)
    return tuple(expectations)


def load_ui_manifest(path: Path) -> tuple[UICase, ...]:
    manifest = path.resolve()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UIEvaluationError("Manifest UI corpus недоступен или повреждён.") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise UIEvaluationError("Manifest UI corpus должен иметь version=1.")
    unknown_root = sorted(set(payload) - {"version", "cases"})
    if unknown_root:
        raise UIEvaluationError(
            f"Manifest содержит неизвестные поля: {', '.join(unknown_root)}."
        )
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= 50:
        raise UIEvaluationError("Manifest должен содержать от 1 до 50 cases.")
    root = manifest.parent
    cases: list[UICase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases):
        field = f"cases[{index}]"
        if not isinstance(raw, Mapping):
            raise UIEvaluationError(f"{field} должен быть объектом.")
        unknown = sorted(
            set(raw)
            - {"id", "task", "screenshot", "expectations", "forbidden_actions", "notes"}
        )
        if unknown:
            raise UIEvaluationError(
                f"{field} содержит неизвестные поля: {', '.join(unknown)}."
            )
        case_id = str(raw.get("id", "")).strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", case_id):
            raise UIEvaluationError(f"{field}.id имеет неверный формат.")
        if case_id in seen:
            raise UIEvaluationError(f"Повторяющийся case id: {case_id}.")
        seen.add(case_id)
        task = raw.get("task")
        if not isinstance(task, str) or not 1 <= len(task.strip()) <= 8000:
            raise UIEvaluationError(f"{field}.task пуст или слишком велик.")
        notes = raw.get("notes")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 1000):
            raise UIEvaluationError(f"{field}.notes должен быть строкой до 1000 символов.")
        expectations = _expectations(raw.get("expectations"), field=f"{field}.expectations")
        forbidden_actions = _action_list(
            raw.get("forbidden_actions"), field=f"{field}.forbidden_actions"
        )
        overlap = sorted({item.action for item in expectations} & set(forbidden_actions))
        if overlap:
            raise UIEvaluationError(
                f"{field} одновременно ожидает и запрещает: {', '.join(overlap)}"
            )
        cases.append(
            UICase(
                case_id=case_id,
                task=task.strip(),
                screenshot=_safe_screenshot(
                    root, raw.get("screenshot"), field=f"{field}.screenshot"
                ),
                expectations=expectations,
                forbidden_actions=forbidden_actions,
            )
        )
    return tuple(cases)


def evaluate_ui_proposal(case: UICase, proposal: ActionProposal) -> UIEvaluationResult:
    arguments: Mapping[str, Any] = proposal.arguments
    action = str(arguments.get("action", "")).casefold()
    reasons: list[str] = []
    if action in case.forbidden_actions:
        reasons.append(f"action {action} явно запрещён")
    alternative_failures: list[str] = []
    matched = False
    for expectation in case.expectations:
        failures: list[str] = []
        if action != expectation.action:
            failures.append(f"action={action or 'empty'} вместо {expectation.action}")
        if expectation.coordinate_region is not None:
            coordinate = arguments.get("coordinate")
            if not isinstance(coordinate, tuple) or len(coordinate) != 2:
                failures.append("нет coordinate")
            else:
                left, top, right, bottom = expectation.coordinate_region
                if not left <= coordinate[0] <= right or not top <= coordinate[1] <= bottom:
                    failures.append(
                        f"coordinate={coordinate} вне {expectation.coordinate_region}"
                    )
        if expectation.keys is not None and arguments.get("keys") != expectation.keys:
            failures.append(f"keys={arguments.get('keys')} вместо {expectation.keys}")
        if not failures:
            matched = True
            break
        alternative_failures.append("; ".join(failures))
    if not matched:
        reasons.append(
            "proposal не соответствует допустимым вариантам: "
            + " | ".join(alternative_failures)
        )
    return UIEvaluationResult(case.case_id, not reasons, action, tuple(reasons))
