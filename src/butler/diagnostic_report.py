from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DiagnosticSummary:
    path: Path
    event_count: int
    invalid_line_count: int
    session_count: int
    levels: dict[str, int]
    components: dict[str, int]
    latest_time: str
    recent_problems: tuple[dict[str, Any], ...]
    active_voice_pid: int | None
    active_voice_event_count: int
    active_voice_problem_count: int
    active_voice_problems: tuple[dict[str, Any], ...]
    legacy_audio_level_count: int


def _event_level(item: dict[str, Any]) -> str:
    raw = str(item.get("level", "unknown")).casefold()
    if (
        item.get("component") == "stt"
        and item.get("event") == "worker_voice_started"
        and raw.replace(".", "", 1).isdigit()
    ):
        return "info"
    return raw


def _log_paths(target: Path) -> list[Path]:
    backups: list[tuple[int, Path]] = []
    for path in target.parent.glob(target.name + ".*"):
        try:
            index = int(path.name.rsplit(".", 1)[-1])
        except ValueError:
            continue
        backups.append((index, path))
    # Highest suffix is oldest; the active file is newest.
    return [path for _, path in sorted(backups, reverse=True)] + [target]


def summarize(runtime_dir: Path, *, recent_limit: int = 10) -> DiagnosticSummary:
    target = runtime_dir / "logs" / "diagnostics.jsonl"
    events: list[dict[str, Any]] = []
    invalid = 0
    for path in _log_paths(target):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            invalid += 1
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if not isinstance(value, dict):
                invalid += 1
                continue
            events.append(value)
    legacy_audio_level_count = sum(
        1
        for item in events
        if item.get("component") == "stt"
        and item.get("event") == "worker_voice_started"
        and str(item.get("level", "")).replace(".", "", 1).isdigit()
    )
    levels = Counter(_event_level(item) for item in events)
    components = Counter(str(item.get("component", "unknown")) for item in events)
    sessions = {str(item.get("session_id", "")) for item in events if item.get("session_id")}
    problems = [
        item for item in events if _event_level(item) in {"warning", "error"}
    ]
    active_voice_pid: int | None = None
    state_path = runtime_dir / "voice" / "agent.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        candidate_pid = int(state.get("pid", 0)) if isinstance(state, dict) else 0
        active_voice_pid = candidate_pid or None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    active_voice_events = (
        [item for item in events if item.get("pid") == active_voice_pid]
        if active_voice_pid is not None
        else []
    )
    active_voice_problems = [
        item
        for item in active_voice_events
        if _event_level(item) in {"warning", "error"}
    ]
    return DiagnosticSummary(
        path=target,
        event_count=len(events),
        invalid_line_count=invalid,
        session_count=len(sessions),
        levels=dict(sorted(levels.items())),
        components=dict(sorted(components.items())),
        latest_time=str(events[-1].get("time", "")) if events else "",
        recent_problems=tuple(problems[-max(1, recent_limit) :]),
        active_voice_pid=active_voice_pid,
        active_voice_event_count=len(active_voice_events),
        active_voice_problem_count=len(active_voice_problems),
        active_voice_problems=tuple(active_voice_problems[-max(1, recent_limit) :]),
        legacy_audio_level_count=legacy_audio_level_count,
    )


def format_summary(summary: DiagnosticSummary) -> str:
    lines = [
        "=== Диагностический журнал Ксении ===",
        f"Файл: {summary.path}",
        f"Событий: {summary.event_count}; сеансов: {summary.session_count}; "
        f"повреждённых строк: {summary.invalid_line_count}.",
        "Уровни: "
        + (", ".join(f"{key}={value}" for key, value in summary.levels.items()) or "нет записей"),
        "Компоненты: "
        + (
            ", ".join(f"{key}={value}" for key, value in summary.components.items())
            or "нет записей"
        ),
        f"Последнее событие: {summary.latest_time or 'нет'}.",
    ]
    if summary.legacy_audio_level_count:
        lines.append(
            "Исторических записей громкости старого формата: "
            f"{summary.legacy_audio_level_count}; они учтены как информационные."
        )
    if summary.active_voice_pid is not None:
        lines.append(
            f"Активный голосовой сеанс: PID={summary.active_voice_pid}; "
            f"событий={summary.active_voice_event_count}; предупреждений и ошибок="
            f"{summary.active_voice_problem_count}."
        )
        if summary.active_voice_problems:
            lines.append("Проблемы активного голосового сеанса:")
            problems_to_render = summary.active_voice_problems
        else:
            lines.append("В активном голосовом сеансе проблем не обнаружено.")
            problems_to_render = ()
        historical_count = max(
            0, sum(summary.levels.get(level, 0) for level in ("warning", "error"))
            - summary.active_voice_problem_count,
        )
        if historical_count:
            lines.append(
                f"В журнале сохранено исторических предупреждений и ошибок: "
                f"{historical_count}."
            )
    elif summary.recent_problems:
        lines.append("Последние предупреждения и ошибки:")
        problems_to_render = summary.recent_problems
    else:
        lines.append("Предупреждений и ошибок пока нет.")
        problems_to_render = ()
    for item in problems_to_render:
        detail = str(item.get("error_type") or item.get("error") or item.get("detail") or "")
        if len(detail) > 180:
            detail = detail[:180] + "…"
        lines.append(
            f"- {item.get('time', '')} | {item.get('component', '')} | "
            f"{item.get('event', '')}" + (f" | {detail}" if detail else "")
        )
    return "\n".join(lines)
