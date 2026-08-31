from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _open_lock_stream(lock_path: Path, target: Path, deadline: float) -> BinaryIO:
    while True:
        try:
            stream = lock_path.open("a+b")
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
            except OSError:
                stream.close()
                raise
            return stream
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Не удалось подготовить файл блокировки: {target}"
                ) from exc
            time.sleep(0.05)


@contextmanager
def exclusive_file_lock(target: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Serialize a file transaction across threads and local processes."""
    target = target.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    local_lock = _thread_lock(lock_path)
    deadline = time.monotonic() + max(0.1, timeout)

    with local_lock, _open_lock_stream(lock_path, target, deadline) as stream:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Не удалось получить блокировку файла: {target}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Не удалось получить блокировку файла: {target}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def atomic_write_text(
    target: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Durably replace a text file through a unique sibling temporary file."""
    if target.is_symlink():
        raise OSError(f"Отказ от атомарной записи через символическую ссылку: {target}")
    target = target.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(target):
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(raw_temporary)
        try:
            with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, target)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def atomic_copy_file(source: Path, target: Path) -> None:
    """Durably replace *target* with a complete copy of *source*."""

    if source.is_symlink():
        raise OSError(f"Отказ от чтения символической ссылки при копировании: {source}")
    source = source.resolve(strict=True)
    if target.is_symlink():
        raise OSError(f"Отказ от атомарной замены символической ссылки: {target}")
    target = target.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(target):
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(raw_temporary)
        try:
            with source.open("rb") as input_stream, os.fdopen(
                descriptor, "wb"
            ) as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            shutil.copystat(source, temporary)
            os.replace(temporary, target)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
