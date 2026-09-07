from __future__ import annotations

import ctypes
import os
import sys
import uuid
import time
from ctypes import wintypes
from pathlib import Path


PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000


class OwnedProcessJob:
    """Own only cooperating child workers; no PID enumeration or host sandbox claim."""

    def __init__(self) -> None:
        self.name = "Local\\KseniaRequest-" + uuid.uuid4().hex
        self.handle = None

    def __enter__(self):
        if os.name != "nt":
            raise OSError("Process-tree ownership requires Windows.")
        try:
            import win32job

            self.handle = win32job.CreateJobObject(None, self.name)
            info = win32job.QueryInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation, info
            )
        except BaseException as exc:
            if self.handle is not None:
                self.handle.Close()
                self.handle = None
            if isinstance(exc, Exception):
                raise OSError("Could not create owned process tree.") from exc
            raise
        return self

    def terminate(self) -> None:
        if self.handle is not None:
            try:
                import win32job

                win32job.TerminateJobObject(self.handle, 1)
                deadline = time.monotonic() + 5
                while win32job.QueryInformationJobObject(
                    self.handle, win32job.JobObjectBasicAccountingInformation
                )["ActiveProcesses"]:
                    if time.monotonic() >= deadline:
                        raise OSError("Owned process tree did not terminate in time.")
                    time.sleep(0.01)
            except Exception as exc:
                raise OSError("Could not terminate owned process tree.") from exc

    def __exit__(self, *_exc) -> None:
        if self.handle is not None:
            self.handle.Close()
            self.handle = None


def join_process_job(name: str) -> None:
    """Join before spawning children; close our handle so the owner controls lifetime."""
    if os.name != "nt" or not name.startswith("Local\\KseniaRequest-"):
        raise OSError("Missing supported process-tree owner.")
    import win32api
    import win32job

    handle = win32job.OpenJobObject(win32job.JOB_OBJECT_ASSIGN_PROCESS, False, name)
    try:
        win32job.AssignProcessToJobObject(handle, win32api.GetCurrentProcess())
    finally:
        handle.Close()


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def process_image_path(pid: int) -> Path | None:
    if os.name != "nt" or pid <= 0:
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).resolve()
    finally:
        kernel32.CloseHandle(handle)


def current_process_image_path() -> Path:
    """Return the real interpreter image, not a virtual-environment launcher."""
    return process_image_path(os.getpid()) or Path(sys.executable).resolve()


def terminate_verified_process(pid: int, expected_executable: Path) -> bool:
    actual = process_image_path(pid)
    if actual is None or actual != expected_executable.resolve():
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        if not kernel32.TerminateProcess(handle, 0):
            return False
        # TerminateProcess is asynchronous. Waiting here prevents the next
        # model or a temporary test directory from racing inherited log/file
        # handles that Windows has not released yet.
        return kernel32.WaitForSingleObject(handle, 5_000) == WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)
