from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from butler.diagnostics import event as diagnostic_event


_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass(frozen=True)
class HandoffItem:
    id: int
    task_id: str
    role: str
    kind: str
    content: str
    metadata: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RoleHandoffStore:
    """Durable task artifacts shared by every model and capability role."""

    def __init__(self, runtime_dir: Path, *, max_items: int = 2_000) -> None:
        self.runtime_dir = runtime_dir
        self.path = runtime_dir / "memory" / "handoffs.sqlite3"
        self.max_items = max(100, int(max_items))

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_handoffs_task ON handoffs(task_id, id)"
        )
        return connection

    @staticmethod
    def _name(value: object, field: str) -> str:
        clean = str(value or "").strip()
        if not _SAFE_NAME.fullmatch(clean):
            raise ValueError(f"Неверное поле {field} для передачи между ролями.")
        return clean

    @staticmethod
    def _item(row: sqlite3.Row) -> HandoffItem:
        try:
            metadata = json.loads(str(row["metadata_json"]))
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return HandoffItem(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            role=str(row["role"]),
            kind=str(row["kind"]),
            content=str(row["content"]),
            metadata=metadata,
            created_at=str(row["created_at"]),
        )

    def append(
        self,
        task_id: object,
        role: object,
        kind: object,
        content: object,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> HandoffItem:
        clean_task = self._name(task_id, "task_id")
        clean_role = self._name(role, "role")
        clean_kind = self._name(kind, "kind")
        clean_content = str(content or "").strip()
        if not clean_content:
            raise ValueError("Пустой материал нельзя передать другой роли.")
        if len(clean_content) > 100_000:
            raise ValueError("Материал передачи длиннее 100000 символов.")
        safe_metadata = metadata if isinstance(metadata, dict) else {}
        metadata_json = json.dumps(safe_metadata, ensure_ascii=False, sort_keys=True)
        if len(metadata_json) > 20_000:
            raise ValueError("Метаданные передачи слишком велики.")
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO handoffs(task_id, role, kind, content, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_task,
                        clean_role,
                        clean_kind,
                        clean_content,
                        metadata_json,
                        created_at,
                    ),
                )
                item_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    DELETE FROM handoffs WHERE id IN (
                        SELECT id FROM handoffs ORDER BY id DESC LIMIT -1 OFFSET ?
                    )
                    """,
                    (self.max_items,),
                )
                row = connection.execute(
                    "SELECT * FROM handoffs WHERE id = ?", (item_id,)
                ).fetchone()
        if row is None:  # pragma: no cover - defensive database guard
            raise RuntimeError("Материал передачи не сохранился.")
        diagnostic_event(
            self.runtime_dir,
            "handoff",
            "artifact_saved",
            task_id=clean_task,
            capability_role=clean_role,
            artifact_kind=clean_kind,
            content_chars=len(clean_content),
            metadata_keys=sorted(str(key) for key in safe_metadata),
        )
        return self._item(row)

    def list_task(self, task_id: object, *, limit: int = 100) -> list[HandoffItem]:
        clean_task = self._name(task_id, "task_id")
        selected_limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM handoffs WHERE task_id = ? ORDER BY id LIMIT ?",
                (clean_task, selected_limit),
            ).fetchall()
        return [self._item(row) for row in rows]

    def render_task(self, task_id: object, *, max_chars: int = 30_000) -> str:
        remaining = max(1_000, int(max_chars))
        sections: list[str] = []
        for item in self.list_task(task_id):
            heading = f"[{item.role}: {item.kind}]"
            available = remaining - len(heading) - 2
            if available <= 0:
                break
            content = item.content[:available]
            sections.append(f"{heading}\n{content}")
            remaining -= len(heading) + len(content) + 2
            if len(content) < len(item.content):
                sections.append("…рабочая память сокращена")
                break
        return "\n\n".join(sections)

    def health(self) -> dict[str, int | str]:
        with closing(self._connect()) as connection:
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            count = int(connection.execute("SELECT count(*) FROM handoffs").fetchone()[0])
        return {"items": count, "integrity": integrity}
