from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from butler.chat import SentenceChunker
from butler.speech import SpeechCompletion, SpeechCompletionCallback
from butler.turn_detection import HybridTurnDetector, TurnDecision


class LiveInterrupted(RuntimeError):
    """Raised at a cooperative checkpoint after the active turn was interrupted."""


class LivePhase(StrEnum):
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    CLOSED = "closed"


class LiveSegmentState(StrEnum):
    QUEUED = "queued"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class LiveTurnToken:
    turn_id: int
    cancel_event: threading.Event

    def checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise LiveInterrupted("Live-реплика прервана.")


@dataclass(frozen=True)
class LiveSpeechToken:
    turn_id: int
    segment_id: int
    text: str


@dataclass(frozen=True)
class LiveSnapshot:
    turn_id: int | None
    phase: LivePhase
    user_text: str
    generated_text: str
    spoken_text: str
    queued_segments: int
    completed_segments: int
    cancelled_segments: int
    failed_segments: int
    generation_finished: bool
    interrupted: bool
    interruption_reason: str


@dataclass
class _LiveSegment:
    segment_id: int
    text: str
    state: LiveSegmentState = LiveSegmentState.QUEUED


@dataclass
class _LiveTurn:
    turn_id: int
    user_text: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    generated_parts: list[str] = field(default_factory=list)
    segments: list[_LiveSegment] = field(default_factory=list)
    generation_finished: bool = False
    interrupted: bool = False
    interruption_reason: str = ""


class TrackedSpeechSink(Protocol):
    def say_tracked(
        self, text: str, on_complete: SpeechCompletionCallback
    ) -> bool: ...

    def stop(self) -> None: ...


def _join_phrases(parts: list[str]) -> str:
    return " ".join(part.strip() for part in parts if part.strip()).strip()


class LiveSession:
    """Thread-safe source of truth for one realtime conversation.

    Generated model text and confirmed playback are deliberately independent.
    A phrase becomes spoken only after the audio backend reports successful
    completion. An interrupted in-flight phrase is conservatively omitted.
    """

    def __init__(self) -> None:
        self._phase = LivePhase.LISTENING
        self._turn: _LiveTurn | None = None
        self._turn_counter = 0
        self._segment_counter = 0
        self._condition = threading.Condition(threading.RLock())

    def begin_turn(self, user_text: str) -> LiveTurnToken:
        clean_text = user_text.strip()
        if not clean_text:
            raise ValueError("Live-реплика пользователя пуста.")
        with self._condition:
            if self._phase == LivePhase.CLOSED:
                raise RuntimeError("Live-сессия уже закрыта.")
            if self._phase in {LivePhase.THINKING, LivePhase.SPEAKING}:
                raise RuntimeError("Предыдущая Live-реплика ещё не завершена.")
            self._turn_counter += 1
            self._turn = _LiveTurn(self._turn_counter, clean_text)
            self._phase = LivePhase.THINKING
            self._condition.notify_all()
            return LiveTurnToken(self._turn.turn_id, self._turn.cancel_event)

    def record_generated(self, turn_id: int, delta: str) -> bool:
        if not delta:
            return True
        with self._condition:
            turn = self._active_turn(turn_id)
            if turn is None or turn.interrupted or turn.generation_finished:
                return False
            turn.generated_parts.append(delta)
            self._condition.notify_all()
            return True

    def queue_segment(self, turn_id: int, text: str) -> LiveSpeechToken | None:
        clean_text = text.strip()
        if not clean_text:
            return None
        with self._condition:
            turn = self._active_turn(turn_id)
            if turn is None or turn.interrupted:
                return None
            self._segment_counter += 1
            segment = _LiveSegment(self._segment_counter, clean_text)
            turn.segments.append(segment)
            self._phase = LivePhase.SPEAKING
            self._condition.notify_all()
            return LiveSpeechToken(turn_id, segment.segment_id, clean_text)

    def complete_segment(
        self,
        token: LiveSpeechToken,
        *,
        ok: bool,
        cancelled: bool,
    ) -> bool:
        with self._condition:
            turn = self._active_turn(token.turn_id)
            if turn is None:
                return False
            segment = next(
                (
                    candidate
                    for candidate in turn.segments
                    if candidate.segment_id == token.segment_id
                ),
                None,
            )
            if segment is None or segment.state != LiveSegmentState.QUEUED:
                return False
            if turn.interrupted or cancelled:
                segment.state = LiveSegmentState.CANCELLED
            elif ok:
                segment.state = LiveSegmentState.COMPLETED
            else:
                segment.state = LiveSegmentState.FAILED
            self._settle_if_ready(turn)
            self._condition.notify_all()
            return True

    def finish_generation(self, turn_id: int) -> bool:
        with self._condition:
            turn = self._active_turn(turn_id)
            if turn is None or turn.interrupted:
                return False
            turn.generation_finished = True
            self._settle_if_ready(turn)
            self._condition.notify_all()
            return True

    def interrupt(self, reason: str = "user_barge_in") -> LiveSnapshot:
        with self._condition:
            turn = self._turn
            if turn is not None and not turn.interrupted:
                # This event is intentionally set before audio stop is invoked by
                # BargeInController. LLM/tool checkpoints therefore close first.
                turn.cancel_event.set()
                turn.interrupted = True
                turn.interruption_reason = reason
                for segment in turn.segments:
                    if segment.state == LiveSegmentState.QUEUED:
                        segment.state = LiveSegmentState.CANCELLED
                self._phase = LivePhase.INTERRUPTED
                self._condition.notify_all()
            return self._snapshot_locked()

    def checkpoint(self, turn_id: int) -> None:
        with self._condition:
            turn = self._active_turn(turn_id)
            if turn is None or turn.cancel_event.is_set():
                raise LiveInterrupted("Live-реплика больше не активна.")

    def snapshot(self) -> LiveSnapshot:
        with self._condition:
            return self._snapshot_locked()

    def wait_until_settled(self, turn_id: int, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._is_settled(turn_id), timeout=timeout
            )

    def close(self) -> None:
        with self._condition:
            if self._turn is not None:
                self._turn.cancel_event.set()
                self._turn.interrupted = True
                for segment in self._turn.segments:
                    if segment.state == LiveSegmentState.QUEUED:
                        segment.state = LiveSegmentState.CANCELLED
            self._phase = LivePhase.CLOSED
            self._condition.notify_all()

    def _active_turn(self, turn_id: int) -> _LiveTurn | None:
        if self._turn is None or self._turn.turn_id != turn_id:
            return None
        return self._turn

    def _settle_if_ready(self, turn: _LiveTurn) -> None:
        if turn.interrupted or not turn.generation_finished:
            return
        if any(
            segment.state == LiveSegmentState.QUEUED for segment in turn.segments
        ):
            return
        self._phase = LivePhase.LISTENING

    def _is_settled(self, turn_id: int) -> bool:
        turn = self._active_turn(turn_id)
        if turn is None:
            return True
        return self._phase in {
            LivePhase.LISTENING,
            LivePhase.INTERRUPTED,
            LivePhase.CLOSED,
        }

    def _spoken_parts(self, turn: _LiveTurn) -> list[str]:
        # Only a contiguous completed prefix is committed. This remains correct
        # even if a future backend happens to deliver callbacks out of order.
        spoken: list[str] = []
        for segment in turn.segments:
            if segment.state != LiveSegmentState.COMPLETED:
                break
            spoken.append(segment.text)
        return spoken

    def _snapshot_locked(self) -> LiveSnapshot:
        turn = self._turn
        if turn is None:
            return LiveSnapshot(
                turn_id=None,
                phase=self._phase,
                user_text="",
                generated_text="",
                spoken_text="",
                queued_segments=0,
                completed_segments=0,
                cancelled_segments=0,
                failed_segments=0,
                generation_finished=False,
                interrupted=False,
                interruption_reason="",
            )
        states = [segment.state for segment in turn.segments]
        return LiveSnapshot(
            turn_id=turn.turn_id,
            phase=self._phase,
            user_text=turn.user_text,
            generated_text="".join(turn.generated_parts),
            spoken_text=_join_phrases(self._spoken_parts(turn)),
            queued_segments=states.count(LiveSegmentState.QUEUED),
            completed_segments=states.count(LiveSegmentState.COMPLETED),
            cancelled_segments=states.count(LiveSegmentState.CANCELLED),
            failed_segments=states.count(LiveSegmentState.FAILED),
            generation_finished=turn.generation_finished,
            interrupted=turn.interrupted,
            interruption_reason=turn.interruption_reason,
        )


class StreamingTTS:
    """Turn model deltas into short, tracked speech requests."""

    def __init__(
        self,
        session: LiveSession,
        speech: TrackedSpeechSink,
        token: LiveTurnToken,
        *,
        minimum_phrase_chars: int = 24,
        maximum_phrase_chars: int = 280,
    ) -> None:
        self.session = session
        self.speech = speech
        self.token = token
        self.chunker = SentenceChunker(
            minimum_length=minimum_phrase_chars,
            maximum_length=maximum_phrase_chars,
        )
        self._finished = False
        self._lock = threading.Lock()

    def feed(self, delta: str) -> bool:
        with self._lock:
            if self._finished or not self.session.record_generated(
                self.token.turn_id, delta
            ):
                return False
            for phrase in self.chunker.feed(delta):
                self._queue(phrase)
            return True

    def finish(self) -> bool:
        with self._lock:
            if self._finished:
                return False
            self._finished = True
            tail = self.chunker.finish()
            if tail:
                self._queue(tail)
            return self.session.finish_generation(self.token.turn_id)

    def _queue(self, phrase: str) -> None:
        speech_token = self.session.queue_segment(self.token.turn_id, phrase)
        if speech_token is None:
            return

        def completed(result: SpeechCompletion) -> None:
            changed = self.session.complete_segment(
                speech_token,
                ok=result.ok,
                cancelled=result.cancelled,
            )
            if changed and (not result.ok or result.cancelled):
                self.session.interrupt(
                    "tts_cancelled" if result.cancelled else "tts_failure"
                )
                self.speech.stop()

        if not self.speech.say_tracked(phrase, completed):
            self.session.complete_segment(
                speech_token,
                ok=False,
                cancelled=False,
            )
            self.session.interrupt("tts_rejected")
            self.speech.stop()


class BargeInController:
    def __init__(self, session: LiveSession, speech: TrackedSpeechSink) -> None:
        self.session = session
        self.speech = speech

    def interrupt(self, reason: str = "user_barge_in") -> LiveSnapshot:
        snapshot = self.session.interrupt(reason)
        self.speech.stop()
        return snapshot


class ConversationCoordinator:
    """Own one LiveSession and coordinate generation, speech and interruption."""

    def __init__(
        self,
        speech: TrackedSpeechSink,
        *,
        minimum_phrase_chars: int = 24,
        maximum_phrase_chars: int = 280,
    ) -> None:
        self.session = LiveSession()
        self.barge_in = BargeInController(self.session, speech)
        self.speech = speech
        self.minimum_phrase_chars = minimum_phrase_chars
        self.maximum_phrase_chars = maximum_phrase_chars
        self._stream: StreamingTTS | None = None
        self._lock = threading.Lock()

    def begin_response(self, user_text: str) -> LiveTurnToken:
        with self._lock:
            token = self.session.begin_turn(user_text)
            self._stream = StreamingTTS(
                self.session,
                self.speech,
                token,
                minimum_phrase_chars=self.minimum_phrase_chars,
                maximum_phrase_chars=self.maximum_phrase_chars,
            )
            return token

    def accept_delta(self, token: LiveTurnToken, delta: str) -> bool:
        with self._lock:
            stream = self._stream
            if stream is None or stream.token.turn_id != token.turn_id:
                return False
            return stream.feed(delta)

    def finish_response(self, token: LiveTurnToken) -> bool:
        with self._lock:
            stream = self._stream
            if stream is None or stream.token.turn_id != token.turn_id:
                return False
            return stream.finish()

    def interrupt(self, reason: str = "user_barge_in") -> LiveSnapshot:
        # Serialize interruption with feed/finish. Otherwise a feed could reserve
        # a segment, observe stop(), and only then submit its audio request.
        with self._lock:
            return self.barge_in.interrupt(reason)

    def snapshot(self) -> LiveSnapshot:
        return self.session.snapshot()

    def close(self) -> None:
        with self._lock:
            self.barge_in.interrupt("session_closed")
            self.session.close()
