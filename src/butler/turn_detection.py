from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class TurnDecision(StrEnum):
    KEEP_LISTENING = "keep_listening"
    END_TURN = "end_turn"


class AdaptiveNoiseGate:
    """Adapt only from samples already classified as non-speech."""

    def __init__(
        self,
        *,
        minimum_threshold: int = 220,
        multiplier: float = 2.2,
        sample_limit: int = 4,
    ) -> None:
        if minimum_threshold <= 0 or not math.isfinite(multiplier) or multiplier <= 1:
            raise ValueError("Некорректные параметры шумового порога.")
        if sample_limit < 1:
            raise ValueError("Для шумового порога нужен хотя бы один образец.")
        self.minimum_threshold = int(minimum_threshold)
        self.multiplier = float(multiplier)
        self.sample_limit = int(sample_limit)
        self._noise_levels: list[int] = []
        self.threshold = self.minimum_threshold

    @property
    def noise_floor(self) -> int:
        if not self._noise_levels:
            return 0
        return round(sum(self._noise_levels) / len(self._noise_levels))

    def observe(self, level: int) -> bool:
        normalized = max(0, int(level))
        voice_active = normalized >= self.threshold
        if not voice_active and len(self._noise_levels) < self.sample_limit:
            self._noise_levels.append(normalized)
            self.threshold = max(
                self.minimum_threshold,
                int(self.noise_floor * self.multiplier),
            )
        return voice_active


class HybridTurnDetector:
    """Combine VAD silence with conservative transcript completeness hints."""

    _incomplete_words = {
        "а",
        "без",
        "в",
        "во",
        "для",
        "до",
        "и",
        "или",
        "из",
        "к",
        "как",
        "на",
        "но",
        "о",
        "об",
        "от",
        "по",
        "под",
        "потому",
        "при",
        "про",
        "с",
        "со",
        "чтобы",
    }
    _hesitation_words = {
        "а",
        "мм",
        "м-м",
        "ну",
        "подожди",
        "секунду",
        "сейчас",
        "так",
        "ээ",
        "эээ",
    }
    _incomplete_phrases = (
        "для того чтобы",
        "потому что",
        "сравни с",
        "так как",
    )

    def __init__(
        self,
        *,
        complete_silence_seconds: float = 0.45,
        ordinary_silence_seconds: float = 0.85,
        incomplete_silence_seconds: float = 2.2,
    ) -> None:
        values = (
            float(complete_silence_seconds),
            float(ordinary_silence_seconds),
            float(incomplete_silence_seconds),
        )
        if not all(math.isfinite(value) for value in values) or not (
            0 < values[0] <= values[1] <= values[2] <= 10
        ):
            raise ValueError(
                "Паузы TurnDetector должны удовлетворять условию "
                "0 < complete <= ordinary <= incomplete <= 10."
            )
        (
            self.complete_silence_seconds,
            self.ordinary_silence_seconds,
            self.incomplete_silence_seconds,
        ) = values
        self._last_voice_at: float | None = None
        self._transcript = ""

    def reset(self) -> None:
        self._last_voice_at = None
        self._transcript = ""

    def required_silence(self, transcript: str) -> float:
        clean = re.sub(r"\s+", " ", transcript.strip().casefold())
        if not clean:
            return self.incomplete_silence_seconds
        words = re.findall(r"[a-zа-яё]+(?:-[a-zа-яё]+)?", clean)
        normalized_words = [word.replace("ё", "е") for word in words]
        normalized = " ".join(normalized_words)
        if not normalized:
            return self.incomplete_silence_seconds
        if (
            normalized_words[-1] in self._incomplete_words
            or any(normalized.endswith(phrase) for phrase in self._incomplete_phrases)
            or set(normalized_words).issubset(self._hesitation_words)
            or clean.endswith(("…", "...", ",", ":", ";", "—", "-"))
        ):
            return self.incomplete_silence_seconds
        if clean.endswith((".", "!", "?")):
            return self.complete_silence_seconds
        return self.ordinary_silence_seconds

    def observe(
        self,
        transcript: str,
        *,
        speech_active: bool,
        at: float,
    ) -> TurnDecision:
        timestamp = float(at)
        if not math.isfinite(timestamp):
            raise ValueError("TurnDetector получил недопустимое время.")
        clean = re.sub(r"\s+", " ", transcript).strip()
        if clean:
            self._transcript = clean
        if speech_active:
            self._last_voice_at = timestamp
            return TurnDecision.KEEP_LISTENING
        if self._last_voice_at is None or not self._transcript:
            return TurnDecision.KEEP_LISTENING
        silence = max(0.0, timestamp - self._last_voice_at)
        if silence >= self.required_silence(self._transcript):
            return TurnDecision.END_TURN
        return TurnDecision.KEEP_LISTENING


class VoskRecognizer(Protocol):
    def AcceptWaveform(self, pcm: bytes) -> bool: ...

    def PartialResult(self) -> str: ...

    def Result(self) -> str: ...


@dataclass(frozen=True)
class TranscriptUpdate:
    text: str
    changed: bool
    finalized: bool


class VoskTranscriptStream:
    """Accumulate Vosk final segments while exposing the newest partial text."""

    def __init__(self, recognizer: VoskRecognizer) -> None:
        self._recognizer = recognizer
        self._final_parts: list[str] = []
        self._partial = ""
        self._latest = ""

    @staticmethod
    def _text(raw: str, key: str) -> str | None:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        value = payload.get(key, "")
        if not isinstance(value, str):
            return None
        return re.sub(r"\s+", " ", value).strip()

    def accept(self, pcm: bytes) -> TranscriptUpdate:
        try:
            finalized = bool(self._recognizer.AcceptWaveform(pcm))
            raw = (
                self._recognizer.Result()
                if finalized
                else self._recognizer.PartialResult()
            )
            value = self._text(raw, "text" if finalized else "partial")
        except Exception:
            return TranscriptUpdate(self._latest, False, False)
        if value is None:
            return TranscriptUpdate(self._latest, False, False)
        if finalized:
            if value:
                self._final_parts.append(value)
            self._partial = ""
        else:
            self._partial = value
        combined = " ".join(
            part for part in (*self._final_parts, self._partial) if part
        ).strip()
        changed = combined != self._latest
        self._latest = combined
        return TranscriptUpdate(combined, changed, finalized)


@dataclass(frozen=True)
class TurnEndpointObservation:
    transcript: str
    transcript_changed: bool
    decision: TurnDecision


class StreamingTurnEndpoint:
    """Feed one streaming transcript into the hybrid end-of-turn detector."""

    def __init__(
        self,
        recognizer: VoskRecognizer,
        detector: HybridTurnDetector | None = None,
    ) -> None:
        self.transcript = VoskTranscriptStream(recognizer)
        self.detector = detector or HybridTurnDetector()

    def observe(
        self,
        pcm: bytes,
        *,
        speech_active: bool,
        at: float,
    ) -> TurnEndpointObservation:
        update = self.transcript.accept(pcm)
        decision = self.detector.observe(
            update.text,
            speech_active=speech_active,
            at=at,
        )
        return TurnEndpointObservation(update.text, update.changed, decision)
