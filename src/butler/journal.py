from __future__ import annotations

import hashlib
import json
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator

from butler.atomic_io import atomic_copy_file, atomic_write_text, exclusive_file_lock


MAX_BACKUP_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class PendingChange:
    operation_id: str
    target: Path
    existed: bool
    backup: Path | None


class ChangeJournal:
    """Recoverable snapshots for assistant-made file writes."""

    def __init__(self, root: Path, runtime_dir: Path) -> None:
        self.root = root.resolve()
        self.directory = runtime_dir / "undo"
        self.index = self.directory / "journal.jsonl"
        self._transaction_state = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize one complete file mutation and its undo journal record."""

        depth = int(getattr(self._transaction_state, "depth", 0))
        if depth:
            self._transaction_state.depth = depth + 1
            try:
                yield
            finally:
                self._transaction_state.depth = depth
            return
        with exclusive_file_lock(self.index):
            self._transaction_state.depth = 1
            try:
                yield
            finally:
                self._transaction_state.depth = 0

    def prepare(self, target: Path) -> PendingChange:
        target = target.resolve()
        operation_id = uuid.uuid4().hex
        existed = target.is_file()
        backup: Path | None = None
        if existed:
            size = target.stat().st_size
            if size > MAX_BACKUP_BYTES:
                raise ValueError("Файл больше 20 МБ: безопасная резервная копия не создана.")
            self.directory.mkdir(parents=True, exist_ok=True)
            backup = self.directory / f"{operation_id}.bak"
            shutil.copy2(target, backup)
        return PendingChange(operation_id, target, existed, backup)

    def commit(self, change: PendingChange) -> str:
        with self.transaction():
            self.directory.mkdir(parents=True, exist_ok=True)
            result_exists = change.target.is_file()
            result_sha256 = (
                hashlib.sha256(change.target.read_bytes()).hexdigest()
                if result_exists
                else ""
            )
            record = {
                "id": change.operation_id,
                "time": datetime.now(timezone.utc).isoformat(),
                "target": str(change.target),
                "existed": change.existed,
                "backup": str(change.backup) if change.backup else "",
                "result_exists": result_exists,
                "result_sha256": result_sha256,
                "undone": False,
            }
            records = self._records()
            records.append(record)
            self._write_records(records)
            return change.operation_id

    def _records(self) -> list[dict[str, object]]:
        try:
            lines = self.index.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records: list[dict[str, object]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    def _write_records(self, records: list[dict[str, object]]) -> None:
        atomic_write_text(
            self.index,
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        )

    def undo_last(self) -> dict[str, str]:
        with self.transaction():
            records = self._records()
            index = next(
                (
                    position
                    for position in range(len(records) - 1, -1, -1)
                    if not records[position].get("undone")
                ),
                None,
            )
            if index is None:
                raise ValueError("Нет изменений Ксении, которые можно отменить.")
            record = records[index]
            raw_target = Path(str(record["target"]))
            if raw_target.is_symlink():
                raise ValueError("Целевой путь отмены подменён символической ссылкой.")
            target = raw_target.resolve()
            if target != self.root and self.root not in target.parents:
                raise ValueError("Журнал содержит путь вне рабочей папки.")
            if "result_exists" not in record:
                raise ValueError(
                    "Старая запись отмены не содержит контрольной суммы; "
                    "безопасный откат остановлен."
                )
            expected_exists = bool(record.get("result_exists"))
            if expected_exists:
                if not target.is_file():
                    raise ValueError(
                        "Файл изменён после действия Ксении; безопасный откат остановлен."
                    )
                current_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
                if current_sha256 != str(record.get("result_sha256", "")):
                    raise ValueError(
                        "Файл изменён после действия Ксении; безопасный откат остановлен."
                    )
            elif target.exists():
                raise ValueError(
                    "Путь создан заново после действия Ксении; безопасный откат остановлен."
                )
            if bool(record.get("existed")):
                backup = Path(str(record.get("backup", "")))
                if backup.is_symlink():
                    raise ValueError("Резервная копия подменена символической ссылкой.")
                if not backup.is_file():
                    raise ValueError("Резервная копия для отмены не найдена.")
                atomic_copy_file(backup, target)
            else:
                target.unlink(missing_ok=True)
            record["undone"] = True
            self._write_records(records)
            return {"operation_id": str(record["id"]), "path": str(target)}
