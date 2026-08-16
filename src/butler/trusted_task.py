from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from butler.diagnostics import event as diagnostic_event


TRUSTED_TASK_TTL_SECONDS = 30 * 60
TRUSTED_TASK_MAX_TTL_SECONDS = 30 * 60
TRUSTED_TASK_STATE_MAX_BYTES = 4_096

TRUSTED_TASK_WARNING = (
    "Доверенная задача отключает повторные подтверждения для следующего запроса. "
    "Ксения сможет изменять и удалять файлы в рабочей папке, запускать разрешённый "
    "код, управлять Windows и браузером, а также отправлять сообщения без дополнительных "
    "вопросов. Запускаемый код работает с правами вашей учётной записи Windows, поэтому включайте "
    "этот режим только для полностью понятной задачи. Вы осознанно принимаете ответственность за "
    "разрешённые действия этой задачи. Встроенные инструменты Ксении по-прежнему запрещают покупки, "
    "платежи, чтение секретов, выход за рабочую папку и обход защит. "
    "Допуск одноразовый и действует не дольше тридцати минут до начала задачи."
)
TRUSTED_TASK_ARMED = (
    "Доверенная задача подготовлена. Следующий запрос не потребует повторных подтверждений. "
    "После начала задачи допуск будет израсходован автоматически."
)
TRUSTED_TASK_STARTED = (
    "Доверенная задача началась. Повторные подтверждения отключены только для неё"
)
TRUSTED_TASK_FINISHED = "Доверенная задача завершена. Обычные подтверждения восстановлены"


@dataclass(frozen=True)
class TrustedTaskGrant:
    grant_id: str
    created_at: datetime
    expires_at: datetime
    created_monotonic: float
    ttl_seconds: int

    def remaining_seconds(self, *, now: datetime, monotonic_now: float) -> int:
        wall_remaining = (self.expires_at - now).total_seconds()
        monotonic_remaining = self.ttl_seconds - (monotonic_now - self.created_monotonic)
        return max(0, int(min(wall_remaining, monotonic_remaining)))


class TrustedTaskStore:
    """One-shot, local-user grant for the next complete routed task.

    The active file is atomically renamed before it is parsed. This makes concurrent
    voice, console and LAN submissions race safely: at most one request can consume
    the grant. Invalid, expired or reboot-stale state always fails closed.
    """

    def __init__(
        self,
        source: object,
        *,
        utc_now: Callable[[], datetime] | None = None,
        monotonic_now: Callable[[], float] | None = None,
    ) -> None:
        runtime_value = getattr(source, "runtime_dir", source)
        self.runtime_dir = Path(runtime_value)
        self.source = source
        self.path = self.runtime_dir / "permissions" / "trusted-next-task.json"
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._monotonic_now = monotonic_now or time.monotonic

    def _now(self) -> datetime:
        value = self._utc_now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def arm(self, *, ttl_seconds: int = TRUSTED_TASK_TTL_SECONDS) -> TrustedTaskGrant:
        ttl = max(1, min(int(ttl_seconds), TRUSTED_TASK_MAX_TTL_SECONDS))
        now = self._now()
        monotonic_now = float(self._monotonic_now())
        grant = TrustedTaskGrant(
            grant_id=uuid.uuid4().hex,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
            created_monotonic=monotonic_now,
            ttl_seconds=ttl,
        )
        payload = {
            "schema_version": 1,
            "grant_id": grant.grant_id,
            "scope": "next_routed_task",
            "activated_by": "local_user",
            "created_at": grant.created_at.isoformat(),
            "expires_at": grant.expires_at.isoformat(),
            "created_monotonic": grant.created_monotonic,
            "ttl_seconds": grant.ttl_seconds,
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > TRUSTED_TASK_STATE_MAX_BYTES:
            raise ValueError("Состояние доверенной задачи превышает безопасный размер.")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        diagnostic_event(
            self.source,
            "trusted_task",
            "grant_armed",
            grant_id=grant.grant_id,
            ttl_seconds=grant.ttl_seconds,
        )
        return grant

    def _decode(self, path: Path) -> TrustedTaskGrant:
        if path.is_symlink():
            raise ValueError("Состояние доверенной задачи не может быть ссылкой.")
        raw = path.read_bytes()
        if not raw or len(raw) > TRUSTED_TASK_STATE_MAX_BYTES:
            raise ValueError("Неверный размер состояния доверенной задачи.")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Неизвестный формат состояния доверенной задачи.")
        if value.get("scope") != "next_routed_task" or value.get("activated_by") != "local_user":
            raise ValueError("Неверная область доверенной задачи.")

        grant_id = str(value.get("grant_id", ""))
        if len(grant_id) != 32 or any(char not in "0123456789abcdef" for char in grant_id):
            raise ValueError("Неверный идентификатор доверенной задачи.")
        created_at = datetime.fromisoformat(str(value.get("created_at", "")))
        expires_at = datetime.fromisoformat(str(value.get("expires_at", "")))
        if created_at.tzinfo is None or expires_at.tzinfo is None:
            raise ValueError("Время доверенной задачи должно содержать часовой пояс.")
        created_at = created_at.astimezone(timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
        created_monotonic = float(value.get("created_monotonic"))
        ttl_seconds = int(value.get("ttl_seconds"))
        if not math.isfinite(created_monotonic) or created_monotonic < 0:
            raise ValueError("Неверная отметка времени доверенной задачи.")
        if not 1 <= ttl_seconds <= TRUSTED_TASK_MAX_TTL_SECONDS:
            raise ValueError("Срок доверенной задачи выходит за безопасный предел.")
        duration = (expires_at - created_at).total_seconds()
        if (
            duration <= 0
            or duration > TRUSTED_TASK_MAX_TTL_SECONDS + 1
            or abs(duration - ttl_seconds) > 1
        ):
            raise ValueError("Неверный срок доверенной задачи.")
        return TrustedTaskGrant(
            grant_id=grant_id,
            created_at=created_at,
            expires_at=expires_at,
            created_monotonic=created_monotonic,
            ttl_seconds=ttl_seconds,
        )

    def _validate_current(self, grant: TrustedTaskGrant) -> bool:
        now = self._now()
        monotonic_now = float(self._monotonic_now())
        # A lower monotonic clock means Windows restarted after activation.
        if monotonic_now + 1 < grant.created_monotonic:
            return False
        if now < grant.created_at - timedelta(minutes=1):
            return False
        return grant.remaining_seconds(now=now, monotonic_now=monotonic_now) > 0

    def _read_current(self, path: Path) -> TrustedTaskGrant | None:
        try:
            grant = self._decode(path)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            diagnostic_event(
                self.source,
                "trusted_task",
                "grant_rejected",
                level="warning",
                error_type=type(exc).__name__,
            )
            return None
        if not self._validate_current(grant):
            diagnostic_event(
                self.source,
                "trusted_task",
                "grant_expired_or_stale",
                level="warning",
                grant_id=grant.grant_id,
            )
            return None
        return grant

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def status(self) -> TrustedTaskGrant | None:
        # Keep this a read-only observation. Removing an invalid file here could
        # race with arm() and accidentally unlink a freshly replaced valid grant.
        # consume() atomically claims and removes stale state; arm() replaces it.
        return self._read_current(self.path)

    def consume(self) -> TrustedTaskGrant | None:
        claim = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.claim"
        )
        try:
            os.replace(self.path, claim)
        except FileNotFoundError:
            return None
        except OSError as exc:
            diagnostic_event(
                self.source,
                "trusted_task",
                "grant_claim_failed",
                level="warning",
                error_type=type(exc).__name__,
            )
            return None
        try:
            grant = self._read_current(claim)
        finally:
            self._unlink(claim)
        if grant is None:
            return None
        diagnostic_event(
            self.source,
            "trusted_task",
            "grant_consumed",
            grant_id=grant.grant_id,
        )
        return grant

    def cancel(self) -> bool:
        claim = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.cancel"
        )
        try:
            os.replace(self.path, claim)
        except FileNotFoundError:
            return False
        self._unlink(claim)
        diagnostic_event(self.source, "trusted_task", "grant_cancelled")
        return True
