from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from butler.atomic_io import atomic_write_text
from butler.config import Settings
from butler.diagnostics import event as diagnostic_event
from butler.local_auth import api_key_file, local_api_key
from butler.processes import process_image_path, terminate_verified_process


QUERY_INSTRUCTION = (
    "Instruct: Retrieve the most relevant passages from local project files and "
    "personal knowledge for the user's Russian query.\nQuery: "
)


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingRuntimeState:
    pid: int
    executable: str
    model: str
    port: int
    started_at: str
    log_path: str


def parse_embedding_response(value: object, expected: int) -> list[list[float]]:
    if not isinstance(value, dict):
        raise EmbeddingError("Сервис эмбеддингов вернул не объект JSON.")
    data = value.get("data")
    if not isinstance(data, list):
        raise EmbeddingError("В ответе эмбеддера отсутствует массив data.")
    ordered: list[tuple[int, list[float]]] = []
    for position, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise EmbeddingError("Эмбеддер вернул повреждённый вектор.")
        try:
            vector = [float(component) for component in item["embedding"]]
            index = int(item.get("index", position))
        except (TypeError, ValueError) as exc:
            raise EmbeddingError("Эмбеддер вернул нечисловой вектор.") from exc
        ordered.append((index, vector))
    ordered.sort(key=lambda item: item[0])
    vectors = [vector for _index, vector in ordered]
    if len(vectors) != expected:
        raise EmbeddingError(
            f"Ожидалось векторов: {expected}, получено: {len(vectors)}."
        )
    if any(not vector for vector in vectors):
        raise EmbeddingError("Эмбеддер вернул пустой вектор.")
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1:
        raise EmbeddingError("Размерности векторов не совпадают.")
    return vectors


class LlamaCppEmbeddingService:
    """Small CPU-only llama.cpp service, shared by indexing and RAG queries."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        config = settings.raw.get("rag", {})
        raw_model = Path(str(config.get("model_path", ""))).expanduser()
        self.model_path = (
            raw_model.resolve()
            if raw_model.is_absolute()
            else (settings.root / raw_model).resolve()
        )
        self.port = int(config.get("port", 18081))
        self.context_size = int(config.get("context_size", 8192))
        self.threads = max(1, int(config.get("cpu_threads", 6)))
        self.expected_size_bytes = max(
            0, int(config.get("expected_size_bytes", 0) or 0)
        )
        self.state_path = settings.runtime_dir / "embedding-state.json"
        self._started_here = False

    @property
    def model_id(self) -> str:
        try:
            size = self.model_path.stat().st_size
        except OSError:
            size = 0
        return f"embedding:{self.model_path.name}:{size}"

    @property
    def _base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def build_command(self) -> list[str]:
        local_api_key(self.settings)
        return [
            str(self.settings.llama_server),
            "--model",
            str(self.model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--embedding",
            "--pooling",
            "last",
            "--embd-normalize",
            "2",
            "--ctx-size",
            str(self.context_size),
            "--batch-size",
            "1024",
            "--ubatch-size",
            "512",
            "--n-gpu-layers",
            "0",
            "--threads",
            str(self.threads),
            "--threads-batch",
            str(self.threads),
            "--parallel",
            "1",
            "--api-key-file",
            str(api_key_file(self.settings)),
            "--no-webui",
        ]

    def _read_state(self) -> EmbeddingRuntimeState | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return EmbeddingRuntimeState(**value)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return None

    def _write_state(self, state: EmbeddingRuntimeState) -> None:
        atomic_write_text(
            self.state_path,
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
        )

    def running_state(self) -> EmbeddingRuntimeState | None:
        state = self._read_state()
        if state is None or state.port != self.port:
            return None
        actual = process_image_path(state.pid)
        if actual != Path(state.executable).resolve():
            return None
        if Path(state.model).resolve() != self.model_path:
            return None
        return state

    def ready(self, timeout: float = 1.0) -> bool:
        request = urllib.request.Request(
            f"{self._base_url}/health",
            headers={"Authorization": f"Bearer {local_api_key(self.settings)}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def _port_open(self, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _wait_port_closed(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._port_open(timeout=0.1):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        return True

    def start(self, *, timeout: float = 90.0) -> EmbeddingRuntimeState:
        state = self.running_state()
        if state is not None and self.ready():
            return state
        if state is not None:
            if not self.stop() or not self._wait_port_closed():
                raise EmbeddingError(
                    "Предыдущий сервис эмбеддингов не освободил порт после остановки."
                )
        if not self.settings.llama_server.is_file():
            raise EmbeddingError(f"Не найден llama-server: {self.settings.llama_server}")
        if not self.model_path.is_file():
            raise EmbeddingError(f"Не найдена модель эмбеддингов: {self.model_path}")
        if (
            self.expected_size_bytes
            and self.model_path.stat().st_size != self.expected_size_bytes
        ):
            raise EmbeddingError(
                f"Файл модели эмбеддингов имеет неверный размер: {self.model_path}. "
                f"Ожидалось {self.expected_size_bytes} байт, "
                f"получено {self.model_path.stat().st_size}. Сервис не запущен."
            )
        if self._port_open():
            raise EmbeddingError(
                f"Порт {self.port} уже занят неизвестным сервисом; процесс не трогаю."
            )
        logs = self.settings.runtime_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = logs / f"llama-embedding-{stamp}.log"
        log_file = log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                self.build_command(),
                cwd=str(self.settings.llama_server.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ),
            )
        finally:
            log_file.close()
        state = EmbeddingRuntimeState(
            pid=process.pid,
            executable=str(self.settings.llama_server.resolve()),
            model=str(self.model_path),
            port=self.port,
            started_at=datetime.now(timezone.utc).isoformat(),
            log_path=str(log_path),
        )
        self._write_state(state)
        self._started_here = True
        started = time.monotonic()
        deadline = started + max(5.0, timeout)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.state_path.unlink(missing_ok=True)
                raise EmbeddingError(
                    f"Сервис эмбеддингов завершился с кодом {process.returncode}. Журнал: {log_path}"
                )
            if self.ready():
                diagnostic_event(
                    self.settings,
                    "embeddings",
                    "service_ready",
                    model_file=self.model_path.name,
                    port=self.port,
                    pid=process.pid,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
                return state
            time.sleep(0.25)
        self.stop()
        raise EmbeddingError(f"Эмбеддер не запустился за {timeout:.0f} секунд: {log_path}")

    def stop(self) -> bool:
        state = self.running_state()
        if state is None:
            return False
        stopped = terminate_verified_process(state.pid, Path(state.executable))
        if stopped:
            self.state_path.unlink(missing_ok=True)
        return stopped

    def _embed_batch(self, inputs: Sequence[str], *, kind: str) -> list[list[float]]:
        request = urllib.request.Request(
            f"{self._base_url}/v1/embeddings",
            data=json.dumps(
                {"input": inputs, "model": self.model_path.name}, ensure_ascii=False
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {local_api_key(self.settings)}",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                value: Any = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1_000]
            raise EmbeddingError(f"Ошибка эмбеддера HTTP {exc.code}: {detail}") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise EmbeddingError(f"Не удалось получить эмбеддинги: {exc}") from exc
        vectors = parse_embedding_response(value, len(inputs))
        diagnostic_event(
            self.settings,
            "embeddings",
            "batch_completed",
            kind=kind,
            text_count=len(inputs),
            input_chars=sum(len(text) for text in inputs),
            dimensions=len(vectors[0]),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return vectors

    def embed(
        self, texts: Sequence[str], *, kind: str = "document"
    ) -> list[list[float]]:
        if kind not in {"document", "query"}:
            raise ValueError("Неизвестный вид текста для эмбеддинга.")
        clean = [str(text).strip() for text in texts]
        if not clean or any(not text for text in clean):
            raise ValueError("Пустой текст нельзя превратить в вектор.")
        self.start()
        inputs = (
            [QUERY_INSTRUCTION + text for text in clean] if kind == "query" else clean
        )
        result: list[list[float]] = []
        batch: list[str] = []
        batch_chars = 0
        for text in inputs:
            if batch and (len(batch) >= 8 or batch_chars + len(text) > 24_000):
                result.extend(self._embed_batch(batch, kind=kind))
                batch = []
                batch_chars = 0
            batch.append(text)
            batch_chars += len(text)
        if batch:
            result.extend(self._embed_batch(batch, kind=kind))
        return result

    def close(self) -> None:
        if self._started_here:
            self.stop()
            self._started_here = False

    def __enter__(self) -> "LlamaCppEmbeddingService":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()
