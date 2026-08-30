from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import time
from array import array
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from butler.diagnostics import event as diagnostic_event
from butler.sensitive_data import is_sensitive_path


TEXT_EXTENSIONS = {
    ".bat",
    ".c",
    ".cc",
    ".cmd",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".ps1",
    ".py",
    ".rs",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".svn",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "runtime",
    "target",
    "venv",
}

SKIP_FILENAMES = {
    ".env",
    ".env.local",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    "secrets.json",
}

SEARCH_STOPWORDS = {
    "а",
    "без",
    "бы",
    "в",
    "во",
    "где",
    "для",
    "до",
    "же",
    "за",
    "и",
    "из",
    "или",
    "как",
    "когда",
    "к",
    "ли",
    "на",
    "не",
    "но",
    "о",
    "об",
    "от",
    "по",
    "под",
    "при",
    "с",
    "со",
    "у",
    "что",
    "это",
    "the",
    "a",
    "an",
    "and",
    "or",
    "in",
    "on",
    "of",
    "to",
    "with",
}


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    def embed(
        self, texts: Sequence[str], *, kind: str = "document"
    ) -> list[list[float]]: ...


@dataclass(frozen=True)
class TextChunk:
    index: int
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True)
class RagResult:
    namespace: str
    path: str
    start_line: int
    end_line: int
    text: str
    score: float
    lexical_rank: int | None
    vector_rank: int | None
    vector_similarity: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class IndexSummary:
    namespace: str
    scanned_files: int
    indexed_files: int
    unchanged_files: int
    removed_files: int
    chunks: int


def chunk_text(
    text: str, *, target_chars: int = 3_200, overlap_lines: int = 6
) -> list[TextChunk]:
    """Split readable text while preserving line citations and small overlap."""
    target_chars = max(8, int(target_chars))
    overlap_lines = max(0, min(int(overlap_lines), 50))
    lines = text.splitlines()
    if not lines and text:
        lines = [text]
    chunks: list[TextChunk] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        selected: list[str] = []
        while end < len(lines):
            line = lines[end]
            projected = size + len(line) + 1
            if selected and projected > target_chars:
                break
            if not selected and len(line) > target_chars:
                # Keep an exceptional generated/minified line intact. Losing its tail is
                # worse than one oversized chunk, and the file-size guard still bounds it.
                selected.append(line)
                size = len(line)
                end += 1
                break
            selected.append(line)
            size = projected
            end += 1
        clean = "\n".join(selected).strip()
        if clean:
            chunks.append(
                TextChunk(
                    index=len(chunks),
                    start_line=start + 1,
                    end_line=max(start + 1, end),
                    text=clean,
                )
            )
        if end >= len(lines):
            break
        next_start = end - overlap_lines
        start = next_start if next_start > start else end
    return chunks


def _normalize(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not values or not math.isfinite(norm) or norm <= 0:
        raise ValueError("Эмбеддер вернул пустой или недопустимый вектор.")
    return [value / norm for value in values]


def _vector_blob(vector: Sequence[float]) -> bytes:
    return array("f", _normalize(vector)).tobytes()


def _vector_from_blob(blob: bytes) -> array[float]:
    values = array("f")
    values.frombytes(blob)
    return values


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return -1.0
    return float(sum(a * b for a, b in zip(left, right)))


class HybridRagIndex:
    """Local project RAG: exact FTS5 retrieval plus semantic vector retrieval."""

    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.path = runtime_dir / "memory" / "rag.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                embedding_model TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                UNIQUE(namespace, path)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                namespace TEXT NOT NULL,
                path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                embedding_dim INTEGER NOT NULL,
                UNIQUE(document_id, chunk_index)
            );
            CREATE INDEX IF NOT EXISTS idx_rag_chunks_namespace ON chunks(namespace, id);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text,
                namespace UNINDEXED,
                path UNINDEXED,
                content='chunks',
                content_rowid='id',
                tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text, namespace, path)
                VALUES (new.id, new.text, new.namespace, new.path);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text, namespace, path)
                VALUES ('delete', old.id, old.text, old.namespace, old.path);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text, namespace, path)
                VALUES ('delete', old.id, old.text, old.namespace, old.path);
                INSERT INTO chunks_fts(rowid, text, namespace, path)
                VALUES (new.id, new.text, new.namespace, new.path);
            END;
            """
        )
        return connection

    def health(self) -> dict[str, int | str]:
        with closing(self._connect()) as connection:
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            documents = int(connection.execute("SELECT count(*) FROM documents").fetchone()[0])
            chunks = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
        return {"documents": documents, "chunks": chunks, "integrity": integrity}

    @staticmethod
    def _namespace(value: object) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip()).strip("-")
        if not clean or len(clean) > 100:
            raise ValueError("Неверное имя пространства RAG.")
        return clean

    def index_text(
        self,
        namespace: object,
        path: object,
        text: str,
        *,
        modified_ns: int,
        embedder: EmbeddingProvider,
        checkpoint: Callable[[], None] | None = None,
    ) -> tuple[bool, int]:
        clean_namespace = self._namespace(namespace)
        clean_path = str(path).replace("\\", "/").strip("/")
        if not clean_path or clean_path.startswith("../") or "/../" in clean_path:
            raise ValueError("Неверный относительный путь для RAG.")
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        chunks = chunk_text(text)
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT id, sha256, embedding_model FROM documents WHERE namespace = ? AND path = ?",
                (clean_namespace, clean_path),
            ).fetchone()
            if (
                existing is not None
                and str(existing["sha256"]) == digest
                and str(existing["embedding_model"]) == embedder.model_id
            ):
                return False, 0
        if checkpoint is not None:
            checkpoint()
        vectors = (
            embedder.embed([chunk.text for chunk in chunks], kind="document")
            if chunks
            else []
        )
        if checkpoint is not None:
            checkpoint()
        if len(vectors) != len(chunks):
            raise ValueError("Число векторов не совпадает с числом фрагментов.")
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM documents WHERE namespace = ? AND path = ?",
                    (clean_namespace, clean_path),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO documents(
                        namespace, path, sha256, size_bytes, modified_ns,
                        embedding_model, indexed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_namespace,
                        clean_path,
                        digest,
                        len(encoded),
                        int(modified_ns),
                        embedder.model_id,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                document_id = int(cursor.lastrowid)
                for chunk, vector in zip(chunks, vectors):
                    normalized = _normalize(vector)
                    connection.execute(
                        """
                        INSERT INTO chunks(
                            document_id, namespace, path, chunk_index, start_line,
                            end_line, text, embedding, embedding_dim
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            clean_namespace,
                            clean_path,
                            chunk.index,
                            chunk.start_line,
                            chunk.end_line,
                            chunk.text,
                            array("f", normalized).tobytes(),
                            len(normalized),
                        ),
                    )
        return True, len(chunks)

    def index_workspace(
        self,
        workspace: Path,
        *,
        namespace: object,
        embedder: EmbeddingProvider,
        max_file_bytes: int = 1_000_000,
        checkpoint: Callable[[], None] | None = None,
    ) -> IndexSummary:
        root = workspace.resolve()
        if not root.is_dir():
            raise ValueError(f"Рабочая папка RAG не найдена: {root}")
        clean_namespace = self._namespace(namespace)
        discovered: set[str] = set()
        scanned = indexed = unchanged = chunks_count = 0
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = sorted(
                [
                    name
                    for name in directories
                    if name.casefold() not in SKIP_DIRECTORIES
                    and not (Path(current) / name).is_symlink()
                ],
                key=str.casefold,
            )
            current_path = Path(current)
            for filename in sorted(filenames, key=str.casefold):
                if checkpoint is not None:
                    checkpoint()
                candidate = current_path / filename
                if candidate.is_symlink():
                    continue
                if filename.casefold() in SKIP_FILENAMES or is_sensitive_path(candidate):
                    continue
                if candidate.suffix.casefold() not in TEXT_EXTENSIONS:
                    continue
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(root)
                    stat = resolved.stat()
                except (OSError, ValueError):
                    continue
                if stat.st_size > max_file_bytes:
                    continue
                scanned += 1
                relative = resolved.relative_to(root).as_posix()
                discovered.add(relative)
                try:
                    raw = resolved.read_bytes()
                    if b"\x00" in raw[:8_192]:
                        continue
                    text = raw.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                changed, count = self.index_text(
                    clean_namespace,
                    relative,
                    text,
                    modified_ns=stat.st_mtime_ns,
                    embedder=embedder,
                    checkpoint=checkpoint,
                )
                if changed:
                    indexed += 1
                    chunks_count += count
                else:
                    unchanged += 1
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT path FROM documents WHERE namespace = ?", (clean_namespace,)
            ).fetchall()
            removed_paths = [str(row["path"]) for row in rows if str(row["path"]) not in discovered]
            with connection:
                for stale in removed_paths:
                    connection.execute(
                        "DELETE FROM documents WHERE namespace = ? AND path = ?",
                        (clean_namespace, stale),
                    )
        summary = IndexSummary(
            namespace=clean_namespace,
            scanned_files=scanned,
            indexed_files=indexed,
            unchanged_files=unchanged,
            removed_files=len(removed_paths),
            chunks=chunks_count,
        )
        diagnostic_event(
            self.runtime_dir,
            "rag",
            "workspace_indexed",
            **asdict(summary),
            embedding_model=embedder.model_id,
        )
        return summary

    @staticmethod
    def _fts_query(query: str) -> str:
        terms: list[str] = []
        seen: set[str] = set()
        for term in re.findall(r"[\w.-]{2,}", query.casefold(), flags=re.UNICODE):
            if term in SEARCH_STOPWORDS or term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= 12:
                break
        return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)

    def search(
        self,
        namespace: object,
        query: object,
        *,
        embedder: EmbeddingProvider,
        limit: int = 8,
        min_vector_similarity: float = 0.3,
        checkpoint: Callable[[], None] | None = None,
    ) -> list[RagResult]:
        clean_namespace = self._namespace(namespace)
        clean_query = re.sub(r"\s+", " ", str(query or "")).strip()
        if not clean_query:
            return []
        started = time.monotonic()
        limit = max(1, min(int(limit), 30))
        min_vector_similarity = float(min_vector_similarity)
        if not math.isfinite(min_vector_similarity) or not 0 <= min_vector_similarity <= 1:
            raise ValueError("Порог смыслового совпадения RAG должен быть от 0 до 1.")
        if checkpoint is not None:
            checkpoint()
        query_vector = _normalize(embedder.embed([clean_query], kind="query")[0])
        if checkpoint is not None:
            checkpoint()
        lexical_ids: list[int] = []
        with closing(self._connect()) as connection:
            fts_query = self._fts_query(clean_query)
            if fts_query:
                lexical_rows = connection.execute(
                    """
                    SELECT rowid FROM chunks_fts
                    WHERE chunks_fts MATCH ? AND namespace = ?
                    ORDER BY bm25(chunks_fts), path COLLATE NOCASE, rowid LIMIT 50
                    """,
                    (fts_query, clean_namespace),
                ).fetchall()
                lexical_ids = [int(row["rowid"]) for row in lexical_rows]
            rows = connection.execute(
                """
                SELECT id, path, start_line, end_line, text, embedding, embedding_dim
                FROM chunks WHERE namespace = ?
                """,
                (clean_namespace,),
            ).fetchall()
        vector_scores: list[tuple[int, float]] = []
        row_by_id: dict[int, sqlite3.Row] = {}
        for index, row in enumerate(rows):
            if checkpoint is not None and index % 256 == 0:
                checkpoint()
            item_id = int(row["id"])
            row_by_id[item_id] = row
            vector = _vector_from_blob(bytes(row["embedding"]))
            if len(vector) != int(row["embedding_dim"]):
                continue
            similarity = _dot(query_vector, vector)
            if math.isfinite(similarity):
                vector_scores.append((item_id, similarity))
        vector_scores.sort(
            key=lambda item: (
                -item[1],
                str(row_by_id[item[0]]["path"]).casefold(),
                int(row_by_id[item[0]]["start_line"]),
                item[0],
            )
        )
        vector_ids = [item_id for item_id, _score in vector_scores[:50]]
        vector_similarity = dict(vector_scores)
        lexical_rank = {item_id: rank for rank, item_id in enumerate(lexical_ids, 1)}
        vector_rank = {item_id: rank for rank, item_id in enumerate(vector_ids, 1)}
        candidate_ids = set(lexical_rank) | set(vector_rank)
        combined: list[tuple[int, float]] = []
        for item_id in candidate_ids:
            if (
                item_id not in lexical_rank
                and vector_similarity.get(item_id, -1.0) < min_vector_similarity
            ):
                continue
            score = 0.0
            if item_id in lexical_rank:
                score += 1.0 / (60 + lexical_rank[item_id])
            if item_id in vector_rank:
                score += 1.0 / (60 + vector_rank[item_id])
            combined.append((item_id, score))
        combined.sort(
            key=lambda item: (
                -item[1],
                str(row_by_id[item[0]]["path"]).casefold(),
                int(row_by_id[item[0]]["start_line"]),
                item[0],
            )
        )
        results: list[RagResult] = []
        for item_id, score in combined[:limit]:
            row = row_by_id.get(item_id)
            if row is None:
                continue
            results.append(
                RagResult(
                    namespace=clean_namespace,
                    path=str(row["path"]),
                    start_line=int(row["start_line"]),
                    end_line=int(row["end_line"]),
                    text=str(row["text"]),
                    score=score,
                    lexical_rank=lexical_rank.get(item_id),
                    vector_rank=vector_rank.get(item_id),
                    vector_similarity=vector_similarity.get(item_id, -1.0),
                )
            )
        diagnostic_event(
            self.runtime_dir,
            "rag",
            "search_completed",
            namespace=clean_namespace,
            query_chars=len(clean_query),
            result_count=len(results),
            candidate_count=len(candidate_ids),
            accepted_candidate_count=len(combined),
            indexed_chunk_count=len(rows),
            duration_ms=round((time.monotonic() - started) * 1000),
            embedding_model=embedder.model_id,
            min_vector_similarity=min_vector_similarity,
        )
        return results
