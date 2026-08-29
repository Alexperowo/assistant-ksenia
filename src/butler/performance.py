from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_REQUIRED_MILESTONES = (
    "voice_start",
    "voice_end",
    "turn_detected",
    "stt_final",
    "llm_request_start",
    "llm_first_token",
    "llm_generation_end",
    "tts_first_chunk_ready",
    "audio_first_played",
    "audio_finished",
)

MILESTONE_PAIRS = (
    ("voice_start", "voice_end", "voice_duration_ms"),
    ("voice_end", "turn_detected", "endpoint_latency_ms"),
    ("turn_detected", "stt_final", "stt_finalization_ms"),
    ("llm_request_start", "llm_first_token", "llm_ttft_ms"),
    ("llm_request_start", "llm_generation_end", "llm_generation_ms"),
    ("llm_first_token", "tts_first_chunk_ready", "text_to_tts_ready_ms"),
    ("tts_first_chunk_ready", "audio_first_played", "tts_ready_to_audio_ms"),
    ("voice_end", "audio_first_played", "voice_end_to_audio_ms"),
    ("interrupt_detected", "audio_actually_stopped", "audio_stop_latency_ms"),
    ("interrupt_detected", "llm_actually_cancelled", "llm_cancel_latency_ms"),
)

_LAST_MILESTONES = frozenset(
    {
        "llm_generation_end",
        "audio_finished",
        "audio_actually_stopped",
        "llm_actually_cancelled",
    }
)
_DIRECT_METRIC_FIELDS = frozenset(
    {
        "duration_ms",
        "first_token_ms",
        "synthesis_ms",
        "playback_ms",
        "capture_seconds",
        "recognition_seconds",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "effective_completion_tokens_per_second",
        "active_reader_threads",
        "cancelled_streams",
        "reader_shutdown_latency_ms",
        "stuck_reader_threads",
    }
)


@dataclass(frozen=True)
class LoadedEvents:
    events: tuple[dict[str, Any], ...]
    invalid_line_count: int
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class MetricStats:
    count: int
    minimum: float
    average: float
    p50: float
    p95: float
    maximum: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "count": self.count,
            "min": _rounded(self.minimum),
            "average": _rounded(self.average),
            "p50": _rounded(self.p50),
            "p95": _rounded(self.p95),
            "max": _rounded(self.maximum),
        }


@dataclass(frozen=True)
class TraceSummary:
    trace_id: str
    task_id: str
    session_id: str
    event_count: int
    duration_ms: float
    milestones_ms: dict[str, float]
    missing_milestones: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "event_count": self.event_count,
            "duration_ms": _rounded(self.duration_ms),
            "milestones_ms": {
                key: _rounded(value) for key, value in self.milestones_ms.items()
            },
            "missing_milestones": list(self.missing_milestones),
        }


@dataclass(frozen=True)
class PerformanceReport:
    event_count: int
    invalid_line_count: int
    untraced_event_count: int
    traces: tuple[TraceSummary, ...]
    metrics: dict[str, MetricStats]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "invalid_line_count": self.invalid_line_count,
            "untraced_event_count": self.untraced_event_count,
            "trace_count": len(self.traces),
            "complete_trace_count": sum(
                not trace.missing_milestones for trace in self.traces
            ),
            "incomplete_trace_count": sum(
                bool(trace.missing_milestones) for trace in self.traces
            ),
            "metrics": {
                key: value.as_dict() for key, value in sorted(self.metrics.items())
            },
            "traces": [trace.as_dict() for trace in self.traces],
        }


def diagnostic_log_paths(runtime_dir: Path) -> list[Path]:
    target = runtime_dir / "logs" / "diagnostics.jsonl"
    backups: list[tuple[int, Path]] = []
    for path in target.parent.glob(target.name + ".*"):
        try:
            index = int(path.name.rsplit(".", 1)[-1])
        except ValueError:
            continue
        backups.append((index, path))
    return [path for _, path in sorted(backups, reverse=True)] + [target]


def load_events(paths: Iterable[Path]) -> LoadedEvents:
    events: list[dict[str, Any]] = []
    invalid = 0
    read_paths: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        read_paths.append(path)
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
    return LoadedEvents(tuple(events), invalid, tuple(read_paths))


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("Нельзя вычислить процентиль пустого набора.")
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("Квантиль должен находиться в диапазоне от 0 до 1.")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def metric_stats(values: Sequence[float]) -> MetricStats:
    if not values:
        raise ValueError("Нельзя свести пустой набор измерений.")
    numeric = [float(value) for value in values]
    return MetricStats(
        count=len(numeric),
        minimum=min(numeric),
        average=statistics.fmean(numeric),
        p50=percentile(numeric, 0.5),
        p95=percentile(numeric, 0.95),
        maximum=max(numeric),
    )


def build_report(
    loaded: LoadedEvents,
    *,
    required_milestones: Sequence[str] = DEFAULT_REQUIRED_MILESTONES,
) -> PerformanceReport:
    traced: dict[str, list[dict[str, Any]]] = {}
    metric_values: dict[str, list[float]] = {}
    untraced = 0
    for item in loaded.events:
        trace_id = str(item.get("trace_id", "")).strip()
        if trace_id:
            traced.setdefault(trace_id, []).append(item)
        else:
            untraced += 1
        for field_name in _DIRECT_METRIC_FIELDS:
            value = _number(item.get(field_name))
            if value is None or value < 0:
                continue
            key = (
                f"{item.get('component', 'unknown')}."
                f"{item.get('event', 'unknown')}.{field_name}"
            )
            metric_values.setdefault(key, []).append(value)

    trace_summaries: list[TraceSummary] = []
    for trace_id, items in traced.items():
        timed = [(item, _event_time_ms(item)) for item in items]
        timed = [(item, stamp) for item, stamp in timed if stamp is not None]
        timed.sort(key=lambda pair: pair[1])
        if not timed:
            continue
        origin = timed[0][1]
        milestone_candidates: dict[str, list[float]] = {}
        for item, stamp in timed:
            if item.get("component") != "performance":
                continue
            name = str(item.get("event", ""))
            offset = max(0.0, stamp - origin)
            milestone_candidates.setdefault(name, []).append(offset)
        milestones = {
            name: (max(values) if name in _LAST_MILESTONES else min(values))
            for name, values in milestone_candidates.items()
        }
        response_anchor = milestones.get(
            "llm_first_token", milestones.get("stt_final", 0.0)
        )
        for name in ("tts_first_chunk_ready", "audio_first_played"):
            eligible = [
                value
                for value in milestone_candidates.get(name, [])
                if value >= response_anchor
            ]
            if eligible:
                milestones[name] = min(eligible)
            else:
                milestones.pop(name, None)
        if "audio_first_played" in milestones:
            eligible_finishes = [
                value
                for value in milestone_candidates.get("audio_finished", [])
                if value >= milestones["audio_first_played"]
            ]
            if eligible_finishes:
                milestones["audio_finished"] = max(eligible_finishes)
            else:
                milestones.pop("audio_finished", None)
        for start_name, end_name, metric_name in MILESTONE_PAIRS:
            if start_name not in milestones or end_name not in milestones:
                continue
            elapsed = milestones[end_name] - milestones[start_name]
            if elapsed >= 0:
                metric_values.setdefault(metric_name, []).append(elapsed)
        missing = tuple(
            name for name in required_milestones if name not in milestones
        )
        first_item = timed[0][0]
        task_id = next(
            (str(item.get("task_id", "")) for item, _ in timed if item.get("task_id")),
            "",
        )
        trace_summaries.append(
            TraceSummary(
                trace_id=trace_id,
                task_id=task_id,
                session_id=str(first_item.get("session_id", "")),
                event_count=len(items),
                duration_ms=max(0.0, timed[-1][1] - origin),
                milestones_ms=milestones,
                missing_milestones=missing,
            )
        )
    trace_summaries.sort(key=lambda item: item.trace_id)
    return PerformanceReport(
        event_count=len(loaded.events),
        invalid_line_count=loaded.invalid_line_count,
        untraced_event_count=untraced,
        traces=tuple(trace_summaries),
        metrics={
            name: metric_stats(values) for name, values in metric_values.items()
        },
    )


def format_report(report: PerformanceReport) -> str:
    complete = sum(not trace.missing_milestones for trace in report.traces)
    lines = [
        "=== Performance / Acceptance Harness Ксении ===",
        f"Событий: {report.event_count}; трасс: {len(report.traces)}; "
        f"полных: {complete}; повреждённых строк: {report.invalid_line_count}; "
        f"событий без trace_id: {report.untraced_event_count}.",
        "",
        "Числовые метрики (единица указана в имени):",
    ]
    if report.metrics:
        for name, stats in sorted(report.metrics.items()):
            values = stats.as_dict()
            lines.append(
                f"- {name}: count={values['count']}, min={values['min']}, "
                f"avg={values['average']}, p50={values['p50']}, "
                f"p95={values['p95']}, max={values['max']}"
            )
    else:
        lines.append("- измерений пока нет")
    if report.traces:
        lines.extend(("", "Трассы:"))
        for trace in report.traces:
            missing = ", ".join(trace.missing_milestones) or "нет"
            lines.append(
                f"- {trace.trace_id}: событий={trace.event_count}, "
                f"длительность={_rounded(trace.duration_ms)} мс, "
                f"отсутствуют={missing}"
            )
    return "\n".join(lines)


def _event_time_ms(item: dict[str, Any]) -> float | None:
    monotonic_ns = _number(item.get("monotonic_ns"))
    if monotonic_ns is not None:
        return monotonic_ns / 1_000_000.0
    value = str(item.get("time", "")).strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _rounded(value: float) -> int | float:
    rounded = round(float(value), 3)
    return int(rounded) if rounded.is_integer() else rounded
