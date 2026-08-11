from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryDecision:
    failure_count: int
    delay_seconds: float
    announce: bool


class RepeatingFailurePolicy:
    """Back off an unattended retry loop without hiding diagnostic attempts."""

    def __init__(
        self,
        *,
        base_delay_seconds: float = 5.0,
        max_delay_seconds: float = 60.0,
        reminder_seconds: float = 300.0,
    ) -> None:
        self.base_delay_seconds = max(0.0, float(base_delay_seconds))
        self.max_delay_seconds = max(
            self.base_delay_seconds, float(max_delay_seconds)
        )
        self.reminder_seconds = max(1.0, float(reminder_seconds))
        self.failure_count = 0
        self.last_announcement_at: float | None = None

    def record_failure(self, now: float) -> RetryDecision:
        self.failure_count += 1
        exponent = min(self.failure_count - 1, 20)
        delay = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2**exponent),
        )
        announce = (
            self.last_announcement_at is None
            or now - self.last_announcement_at >= self.reminder_seconds
        )
        if announce:
            self.last_announcement_at = now
        return RetryDecision(self.failure_count, delay, announce)

    def reset(self) -> None:
        self.failure_count = 0
        self.last_announcement_at = None
