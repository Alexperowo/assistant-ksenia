from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from butler.config import Settings
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import exception as diagnostic_exception
from butler.diagnostics import milestone as diagnostic_milestone
from butler.diagnostics import new_trace_id
from butler.handoff import RoleHandoffStore
from butler.research import ResearchCoordinator
from butler.tasking import DurableTaskStore, TaskCancelled, TaskControl, TaskState
from butler.tools import ToolExecutor

StatusCallback = Callable[[str], None]


@dataclass(frozen=True)
class BackgroundResearchResult:
    task_id: str
    request: str
    answer: str
    tool_events: tuple[Any, ...] = ()
    error: str = ""
    cancelled: bool = False
    trace_id: str = ""


class IsolatedResearchSession:
    """Isolated tool and state container for a background research task."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tools = ToolExecutor(settings)


def is_research_status_command(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in normalized)
    joined = " ".join(cleaned.split())
    signals = (
        "что нашла",
        "что нашел",
        "как там поиск",
        "как поиск",
        "что с поиском",
        "статус поиска",
        "результаты поиска",
        "результат поиска",
        "как успехи с поиском",
        "прогресс поиска",
    )
    return any(sig in joined for sig in signals)


def is_research_cancel_command(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in normalized)
    joined = " ".join(cleaned.split())
    signals = (
        "отмени поиск",
        "отменить поиск",
        "останови поиск",
        "остановить поиск",
        "хватит искать",
        "прекрати поиск",
        "отмени исследование",
        "отменить исследование",
    )
    return any(sig in joined for sig in signals)


def is_research_pause_command(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in normalized)
    joined = " ".join(cleaned.split())
    signals = (
        "подожди поиск",
        "пауза поиска",
        "приостанови поиск",
        "приостановить поиск",
    )
    return any(sig in joined for sig in signals)


def is_research_resume_command(text: str) -> bool:
    normalized = " ".join(text.casefold().replace("ё", "е").split())
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in normalized)
    joined = " ".join(cleaned.split())
    signals = (
        "продолжи поиск",
        "продолжить поиск",
        "возобнови поиск",
        "возобновить поиск",
    )
    return any(sig in joined for sig in signals)


class BackgroundResearchManager:
    """Manage exactly one isolated background read-only research task."""

    def __init__(
        self,
        settings: Settings,
        handoffs: RoleHandoffStore,
        task_store: DurableTaskStore,
    ) -> None:
        self.settings = settings
        self.handoffs = handoffs
        self.task_store = task_store
        self._lock = threading.Lock()
        self._active_task_id: str | None = None
        self._active_request: str = ""
        self._worker_thread: threading.Thread | None = None
        self._result_queue: queue.Queue[BackgroundResearchResult] = queue.Queue()
        self._latest_status: str = ""

    def is_busy(self) -> bool:
        with self._lock:
            return (
                self._active_task_id is not None
                and self._worker_thread is not None
                and self._worker_thread.is_alive()
            )

    def get_active_task_id(self) -> str | None:
        with self._lock:
            if (
                self._active_task_id is not None
                and self._worker_thread is not None
                and self._worker_thread.is_alive()
            ):
                return self._active_task_id
            return None

    def get_active_request(self) -> str:
        with self._lock:
            return self._active_request

    def get_status(self) -> str:
        with self._lock:
            task_id = self._active_task_id
            request = self._active_request
            status_text = self._latest_status
            thread_alive = self._worker_thread is not None and self._worker_thread.is_alive()

        if not task_id or not thread_alive:
            return "Сейчас нет активного поиска."

        record = self.task_store.get(task_id)
        if record:
            state = record.get("state", "")
            if state == TaskState.PAUSED:
                return f"Поиск по запросу «{request}» приостановлен."
            persisted_status = str(record.get("status", "")).strip()
            if persisted_status:
                status_text = persisted_status

        if status_text:
            return f"Поиск по запросу «{request}» выполняется: {status_text}."
        return f"Поиск по запросу «{request}» выполняется."

    def cancel_active(self) -> bool:
        with self._lock:
            task_id = self._active_task_id
            thread_alive = self._worker_thread is not None and self._worker_thread.is_alive()
        if not task_id or not thread_alive:
            return False
        try:
            self.task_store.cancel(task_id)
            diagnostic_event(
                self.settings,
                "background_research",
                "cancel_requested",
                task_id=task_id,
            )
            return True
        except (KeyError, OSError, ValueError):
            return False

    def pause_active(self) -> bool:
        with self._lock:
            task_id = self._active_task_id
            thread_alive = self._worker_thread is not None and self._worker_thread.is_alive()
        if not task_id or not thread_alive:
            return False
        try:
            self.task_store.request_pause(task_id)
            diagnostic_event(
                self.settings,
                "background_research",
                "pause_requested",
                task_id=task_id,
            )
            return True
        except (KeyError, OSError, ValueError):
            return False

    def resume_active(self) -> bool:
        with self._lock:
            task_id = self._active_task_id
            thread_alive = self._worker_thread is not None and self._worker_thread.is_alive()
        if not task_id or not thread_alive:
            return False
        try:
            self.task_store.resume(task_id)
            diagnostic_event(
                self.settings,
                "background_research",
                "resume_requested",
                task_id=task_id,
            )
            return True
        except (KeyError, OSError, ValueError):
            return False

    def poll_completed_result(self) -> BackgroundResearchResult | None:
        try:
            return self._result_queue.get_nowait()
        except queue.Empty:
            return None

    def start_research(
        self,
        request: str,
        *,
        assistant_mode: str | None = None,
        on_status: StatusCallback | None = None,
        trace_id: str | None = None,
    ) -> str:
        with self._lock:
            if (
                self._active_task_id is not None
                and self._worker_thread is not None
                and self._worker_thread.is_alive()
            ):
                raise RuntimeError("Фоновый поиск уже выполняется.")

            record = self.task_store.create(request, channel="voice")
            self.task_store.transition(
                record.id, TaskState.RUNNING, "Начинаю исследование"
            )
            task_id = record.id
            self._active_task_id = task_id
            self._active_request = request
            self._latest_status = "Начинаю исследование"
            active_trace_id = trace_id or new_trace_id()

        diagnostic_milestone(
            self.settings,
            "background_research_queued",
            task_id=task_id,
            trace_id=active_trace_id,
        )

        def _worker() -> None:
            started = time.monotonic()
            diagnostic_milestone(
                self.settings,
                "background_research_started",
                task_id=task_id,
                trace_id=active_trace_id,
            )
            control = TaskControl(self.task_store, task_id, trace_id=active_trace_id)
            self.handoffs.append(
                task_id,
                "assistant",
                "request",
                request,
                metadata={"durable_task": True, "background": True},
            )
            isolated_session = IsolatedResearchSession(self.settings)
            research_model = self.settings.capability_model(
                "researcher", fallback="assistant"
            )
            research = ResearchCoordinator(self.settings, research_model)

            def _track_status(message: str) -> None:
                self._latest_status = message
                try:
                    control.status(TaskState.RUNNING, message)
                except Exception:
                    pass
                if on_status is not None:
                    try:
                        on_status(message)
                    except Exception:
                        pass

            diagnostic_event(
                self.settings,
                "background_research",
                "started",
                task_id=task_id,
                request=request,
                assistant_mode=assistant_mode,
            )

            try:
                reply = research.run(
                    request,
                    isolated_session,  # type: ignore[arg-type]
                    confirmed=False,
                    on_status=_track_status,
                    control=control,
                    assistant_mode=assistant_mode,
                )
                self.task_store.transition(
                    task_id,
                    TaskState.COMPLETED,
                    "Готово",
                    answer=reply.text,
                    generated_answer=reply.text,
                )
                self.handoffs.append(
                    task_id,
                    "researcher",
                    "result",
                    reply.text,
                    metadata={
                        "model_role": research_model,
                        "tool_event_count": len(reply.tool_events),
                        "background": True,
                    },
                )
                diagnostic_event(
                    self.settings,
                    "background_research",
                    "completed",
                    task_id=task_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    answer_chars=len(reply.text),
                    tool_event_count=len(reply.tool_events),
                )
                diagnostic_milestone(
                    self.settings,
                    "background_research_completed",
                    task_id=task_id,
                    trace_id=active_trace_id,
                )
                self._result_queue.put(
                    BackgroundResearchResult(
                        task_id=task_id,
                        request=request,
                        answer=reply.text,
                        tool_events=tuple(reply.tool_events),
                        trace_id=active_trace_id,
                    )
                )
            except TaskCancelled:
                diagnostic_event(
                    self.settings,
                    "background_research",
                    "cancelled",
                    task_id=task_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                diagnostic_milestone(
                    self.settings,
                    "background_research_cancelled",
                    task_id=task_id,
                    trace_id=active_trace_id,
                )
                current = self.task_store.get(task_id)
                if current and current.get("state") != TaskState.CANCELLED:
                    try:
                        self.task_store.cancel(task_id)
                    except Exception:
                        pass
                self._result_queue.put(
                    BackgroundResearchResult(
                        task_id=task_id,
                        request=request,
                        answer="",
                        cancelled=True,
                        trace_id=active_trace_id,
                    )
                )
            except Exception as exc:
                diagnostic_exception(
                    self.settings,
                    "background_research",
                    "failed",
                    exc,
                    task_id=task_id,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                diagnostic_milestone(
                    self.settings,
                    "background_research_failed",
                    task_id=task_id,
                    trace_id=active_trace_id,
                )
                err_text = str(exc) or "Не удалось завершить поиск"
                try:
                    self.task_store.transition(
                        task_id,
                        TaskState.FAILED,
                        "Ошибка поиска",
                        error=err_text,
                    )
                except Exception:
                    pass
                self._result_queue.put(
                    BackgroundResearchResult(
                        task_id=task_id,
                        request=request,
                        answer="",
                        error=err_text,
                        trace_id=active_trace_id,
                    )
                )
            finally:
                with self._lock:
                    if self._active_task_id == task_id:
                        self._active_task_id = None
                        self._active_request = ""
                        self._worker_thread = None

        thread = threading.Thread(
            target=_worker,
            name=f"background-research-{task_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._worker_thread = thread
        thread.start()
        return task_id
