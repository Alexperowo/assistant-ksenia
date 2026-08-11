from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path


ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """A crash-safe named Windows mutex for one exclusive assistant activity."""

    def __init__(self, root: Path, purpose: str) -> None:
        identity = hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:16]
        safe_purpose = "".join(character for character in purpose if character.isalnum() or character == "-")
        self.name = f"Local\\Ksenia-{safe_purpose}-{identity}"
        self.handle: int | None = None
        self.acquired = False

    def acquire(self) -> bool:
        if os.name != "nt":
            self.acquired = True
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        ctypes.set_last_error(0)
        handle = create_mutex(None, 0, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Не удалось создать блокировку Ксении.")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            close_handle(handle)
            return False
        self.handle = int(handle)
        self.acquired = True
        return True

    def release(self) -> None:
        if self.handle is not None and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = None
        self.acquired = False

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
