from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class KnowledgeItem:
    id: int
    text: str
    category: str
    source: str
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeStore:
    """Small local fact store, separate from transient conversation history."""

    def __init__(self, runtime_dir: Path) -> None:
        self.path = runtime_dir / "memory" / "knowledge.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                normalized TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL DEFAULT 'общее',
                source TEXT NOT NULL DEFAULT 'пользователь',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        return connection

    @staticmethod
    def _clean(text: object, *, maximum: int) -> str:
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            raise ValueError("Пустую запись нельзя сохранить.")
        if len(value) > maximum:
            raise ValueError(f"Запись длиннее допустимых {maximum} символов.")
        return value

    @staticmethod
    def _item(row: sqlite3.Row) -> KnowledgeItem:
        return KnowledgeItem(
            id=int(row["id"]),
            text=str(row["text"]),
            category=str(row["category"]),
            source=str(row["source"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def remember(
        self,
        text: object,
        *,
        category: object = "общее",
        source: object = "пользователь",
    ) -> KnowledgeItem:
        clean_text = self._clean(text, maximum=4000)
        clean_category = self._clean(category, maximum=80)
        clean_source = self._clean(source, maximum=200)
        normalized = clean_text.casefold()
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            with connection:
                existing = connection.execute(
                    "SELECT id, created_at FROM knowledge WHERE normalized = ?", (normalized,)
                ).fetchone()
                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO knowledge(text, normalized, category, source, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (clean_text, normalized, clean_category, clean_source, now, now),
                    )
                    item_id = int(cursor.lastrowid)
                else:
                    item_id = int(existing["id"])
                    connection.execute(
                        """
                        UPDATE knowledge
                        SET text = ?, category = ?, source = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (clean_text, clean_category, clean_source, now, item_id),
                    )
                row = connection.execute(
                    "SELECT id, text, category, source, created_at, updated_at FROM knowledge WHERE id = ?",
                    (item_id,),
                ).fetchone()
        if row is None:  # pragma: no cover - defensive database guard
            raise RuntimeError("Запись не сохранилась.")
        return self._item(row)

    def search(self, query: object = "", *, limit: int = 12) -> list[KnowledgeItem]:
        clean_query = re.sub(r"\s+", " ", str(query or "")).strip().casefold()
        limit = min(50, max(1, int(limit)))
        with closing(self._connect()) as connection:
            if clean_query:
                words = [word for word in re.findall(r"[\w-]+", clean_query) if len(word) > 1]
                if not words:
                    words = [clean_query]
                words = words[:8]
                clauses = " AND ".join(
                    "(normalized LIKE ? OR lower(category) LIKE ? OR lower(source) LIKE ?)"
                    for _ in words
                )
                parameters: list[object] = []
                for word in words:
                    pattern = f"%{word}%"
                    parameters.extend((pattern, pattern, pattern))
                rows = connection.execute(
                    f"""
                    SELECT id, text, category, source, created_at, updated_at
                    FROM knowledge WHERE {clauses}
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (*parameters, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, text, category, source, created_at, updated_at
                    FROM knowledge ORDER BY updated_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._item(row) for row in rows]

    def forget(self, item_id: object) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute("DELETE FROM knowledge WHERE id = ?", (int(item_id),))
                return cursor.rowcount == 1

    def health(self) -> dict[str, int | str]:
        with closing(self._connect()) as connection:
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            count = int(connection.execute("SELECT count(*) FROM knowledge").fetchone()[0])
        return {"items": count, "integrity": integrity}
