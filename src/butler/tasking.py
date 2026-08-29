from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from butler.atomic_io import atomic_write_text, exclusive_file_lock
from butler.processes import process_image_path
from butler.diagnostics import event as diagnostic_event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return process_image_path(pid) is not None
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class TaskState(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    PAUSED = "paused"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_STATES = {
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELLED,
}


@dataclass
class TaskEvent:
    state: str
    status: str
    at: str = field(default_factory=_now)


@dataclass
class TaskRecord:
    id: str
    request: str
    channel: str
    state: str = TaskState.QUEUED
    status: str = "В очереди"
    answer: str = ""
    generated_answer: str = ""
    spoken_answer: str = ""
    error: str = ""
    confirmation: dict[str, Any] | None = None
    resumable: bool = True
    owner_pid: int = 0
    revision: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    events: list[TaskEvent] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        value = asdict(self)
        value["done"] = TaskState(self.state) in TERMINAL_STATES
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskRecord":
        events = [
            TaskEvent(**item)
            for item in value.get("events", [])
            if isinstance(item, dict)
        ]
        fields = {
            key: value[key]
            for key in cls.__dataclass_fields__
            if key in value and key != "events"
        }
        return cls(**fields, events=events)


class TaskCancelled(RuntimeError):
    pass


class DurableTaskStore:
    """Crash-tolerant local task journal with pause and cancellation controls."""

    def __init__(self, runtime_dir: Path, *, max_tasks: int = 200) -> None:
        self.runtime_dir = runtime_dir
        self.root = runtime_dir / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_tasks = max(20, max_tasks)
        self._lock = threading.RLock()
        recovered = self.recover_interrupted()
        diagnostic_event(
            self.runtime_dir,
            "tasks",
            "store_ready",
            recovered_count=recovered,
            max_tasks=self.max_tasks,
        )

    def _path(self, task_id: str) -> Path:
        if not task_id or any(character not in "0123456789abcdef" for character in task_id):
            raise KeyError("Неверный идентификатор задачи.")
        return self.root / f"{task_id}.json"

    def _write(self, record: TaskRecord) -> None:
        target = self._path(record.id)
        atomic_write_text(
            target,
            json.dumps(record.snapshot(), ensure_ascii=False, indent=2),
        )

    def _read(self, task_id: str) -> TaskRecord:
        target = self._path(task_id)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(task_id) from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Повреждён журнал задачи: {target}")
        return TaskRecord.from_dict(raw)

    def create(self, request: str, *, channel: str) -> TaskRecord:
        text = request.strip()
        if not text:
            raise ValueError("Пустая задача не создаётся.")
        record = TaskRecord(id=uuid.uuid4().hex, request=text, channel=channel)
        record.events.append(TaskEvent(record.state, record.status, record.created_at))
        with self._lock:
            self._write(record)
            self._prune()
        diagnostic_event(
            self.runtime_dir,
            "tasks",
            "created",
            task_id=record.id,
            channel=channel,
            request=text,
        )
        return record

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                return self._read(task_id).snapshot()
            except KeyError:
                return None

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            paths = sorted(
                self.root.glob("*.json"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            result: list[dict[str, Any]] = []
            for path in paths[: max(1, min(limit, self.max_tasks))]:
                try:
                    result.append(self._read(path.stem).snapshot())
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            return result

    def transition(
        self,
        task_id: str,
        state: TaskState,
        status: str,
        *,
        answer: str = "",
        generated_answer: str | None = None,
        spoken_answer: str | None = None,
        error: str = "",
        confirmation: dict[str, Any] | None | object = ...,
        resumable: bool | None = None,
        expected_revision: int | None = None,
        expected_states: set[TaskState] | None = None,
    ) -> dict[str, Any]:
        with self._lock, exclusive_file_lock(self._path(task_id)):
            record = self._read(task_id)
            current_state = TaskState(record.state)
            if expected_revision is not None and record.revision != expected_revision:
                raise ValueError(
                    f"Состояние задачи изменилось: ожидалась ревизия {expected_revision}, "
                    f"получена {record.revision}."
                )
            if expected_states is not None and current_state not in expected_states:
                allowed = ", ".join(sorted(str(item) for item in expected_states))
                raise ValueError(
                    f"Переход из состояния {current_state} запрещён; ожидалось: {allowed}."
                )
            if current_state in TERMINAL_STATES and state != current_state:
                raise ValueError(
                    f"Завершённая задача {current_state} не может перейти в {state}."
                )
            record.state = state
            record.status = status.strip() or record.status
            record.updated_at = _now()
            if answer:
                record.answer = answer
            if generated_answer is not None:
                record.generated_answer = generated_answer
            if spoken_answer is not None:
                record.spoken_answer = spoken_answer
            if error:
                record.error = error
            if confirmation is not ...:
                record.confirmation = confirmation  # type: ignore[assignment]
            if resumable is not None:
                record.resumable = resumable
            record.owner_pid = 0 if state in TERMINAL_STATES else os.getpid()
            record.revision += 1
            if (
                not record.events
                or record.events[-1].state != state
                or record.events[-1].status != record.status
            ):
                record.events.append(TaskEvent(state, record.status, record.updated_at))
            self._write(record)
            snapshot = record.snapshot()
        diagnostic_event(
            self.runtime_dir,
            "tasks",
            "transition",
            task_id=task_id,
            state=str(state),
            status=record.status,
            has_answer=bool(answer),
            generated_answer_chars=(
                len(generated_answer) if generated_answer is not None else None
            ),
            spoken_answer_chars=(
                len(spoken_answer) if spoken_answer is not None else None
            ),
            has_error=bool(error),
            waiting_confirmation=record.confirmation is not None,
            resumable=record.resumable,
        )
        return snapshot

    def request_pause(self, task_id: str) -> dict[str, Any]:
        return self.transition(task_id, TaskState.PAUSED, "Приостановлено")

    def resume(self, task_id: str) -> dict[str, Any]:
        current = self.get(task_id)
        if current is None:
            raise KeyError(task_id)
        if not current.get("resumable", False):
            raise ValueError("Эту задачу нельзя продолжить.")
        return self.transition(task_id, TaskState.RUNNING, "Продолжаю")

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self.transition(
            task_id,
            TaskState.CANCELLED,
            "Отменено",
            confirmation=None,
            resumable=False,
        )

    def checkpoint(self, task_id: str, *, poll_seconds: float = 0.2) -> None:
        while True:
            current = self.get(task_id)
            if current is None:
                raise TaskCancelled("Журнал задачи удалён.")
            state = TaskState(str(current["state"]))
            if state == TaskState.CANCELLED:
                raise TaskCancelled("Задача отменена Александром.")
            if state != TaskState.PAUSED:
                return
            time.sleep(max(0.05, min(poll_seconds, 1.0)))

    def recover_interrupted(self) -> int:
        recovered = 0
        active = {
            TaskState.PLANNING,
            TaskState.RUNNING,
            TaskState.WAITING_CONFIRMATION,
            TaskState.VERIFYING,
        }
        with self._lock:
            for path in self.root.glob("*.json"):
                try:
                    with exclusive_file_lock(path):
                        record = self._read(path.stem)
                        if TaskState(record.state) not in active:
                            continue
                        if _process_alive(record.owner_pid):
                            continue
                        record.state = TaskState.INTERRUPTED
                        record.status = "Прервано при завершении программы"
                        record.confirmation = None
                        record.owner_pid = 0
                        record.revision += 1
                        record.updated_at = _now()
                        record.events.append(
                            TaskEvent(record.state, record.status, record.updated_at)
                        )
                        self._write(record)
                        recovered += 1
                except (KeyError, OSError, ValueError, json.JSONDecodeError):
                    continue
        if recovered:
            diagnostic_event(
                self.runtime_dir,
                "tasks",
                "interrupted_recovered",
                recovered_count=recovered,
            )
        return recovered

    def _prune(self) -> None:
        paths = sorted(self.root.glob("*.json"), key=lambda item: item.stat().st_mtime)
        excess = len(paths) - self.max_tasks
        if excess <= 0:
            return
        removable: list[Path] = []
        for path in paths:
            try:
                state = TaskState(self._read(path.stem).state)
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                removable.append(path)
                continue
            if state in TERMINAL_STATES:
                removable.append(path)
        removed = 0
        for path in removable[:excess]:
            path.unlink(missing_ok=True)
            removed += 1
        if removed:
            diagnostic_event(
                self.runtime_dir,
                "tasks",
                "old_records_pruned",
                removed_count=removed,
                retained_limit=self.max_tasks,
            )


class TaskControl:
    def __init__(self, store: DurableTaskStore, task_id: str) -> None:
        self.store = store
        self.task_id = task_id

    def checkpoint(self) -> None:
        self.store.checkpoint(self.task_id)

    def status(self, state: TaskState, text: str) -> None:
        self.store.transition(self.task_id, state, text)
