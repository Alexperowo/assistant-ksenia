from __future__ import annotations

import atexit
import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

from butler.diagnostics import event as diagnostic_event


MEDIA_KEYS = {
    0xAD: "volume_mute",
    0xAE: "volume_down",
    0xAF: "volume_up",
    0xB0: "next_track",
    0xB1: "previous_track",
    0xB2: "media_stop",
    0xB3: "play_pause",
    0xB5: "media_select",
    0xB6: "launch_app_1",
    0xB7: "launch_app_2",
}

BUTTON_LABELS = {
    "volume_mute": "отключение звука",
    "volume_down": "уменьшение громкости",
    "volume_up": "увеличение громкости",
    "next_track": "следующий трек",
    "previous_track": "предыдущий трек",
    "media_stop": "остановка воспроизведения",
    "play_pause": "воспроизведение или пауза",
    "media_select": "выбор медиаприложения",
    "launch_app_1": "дополнительная кнопка приложения один",
    "launch_app_2": "дополнительная кнопка приложения два",
}

SUITABLE_ACTIVATION_BUTTONS = {
    "play_pause",
    "next_track",
    "previous_track",
    "media_stop",
    "media_select",
    "launch_app_1",
    "launch_app_2",
}


@dataclass(frozen=True)
class MediaButtonEvent:
    name: str
    vk_code: int
    received_at: float


class _KbdLlHookStruct(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MediaButtonListener:
    """Observe Bluetooth AVRCP media keys through a Windows low-level hook."""

    def __init__(
        self,
        diagnostics_source: object,
        *,
        buttons: set[str] | None = None,
        consume: bool = False,
        debounce_ms: int = 700,
    ) -> None:
        self.diagnostics_source = diagnostics_source
        self.buttons = set(buttons or MEDIA_KEYS.values())
        self.consume = consume
        self.debounce_seconds = max(0.1, debounce_ms / 1000)
        self.events: queue.Queue[MediaButtonEvent] = queue.Queue()
        self._last_at: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._started = False
        self._thread_id = 0
        self._hook = None
        self._callback = None
        self.error = ""
        atexit.register(self.stop)

    def record_virtual_key(
        self, vk_code: int, *, received_at: float | None = None
    ) -> MediaButtonEvent | None:
        name = MEDIA_KEYS.get(int(vk_code))
        if name is None or name not in self.buttons:
            return None
        now = time.monotonic() if received_at is None else float(received_at)
        if now - self._last_at.get(name, -10.0) < self.debounce_seconds:
            return None
        self._last_at[name] = now
        event = MediaButtonEvent(name, int(vk_code), now)
        self.events.put(event)
        diagnostic_event(
            self.diagnostics_source,
            "headset",
            "media_button_received",
            button=name,
            vk_code=vk_code,
        )
        return event

    def start(self, *, timeout: float = 3.0) -> bool:
        if os.name != "nt":
            self.error = "Мультимедийные кнопки поддерживаются только в Windows."
            return False
        if self._thread is not None and self._thread.is_alive():
            return self._started
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=timeout)
        return self._started

    def _run(self) -> None:  # pragma: no cover - requires a real Windows input event
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        wh_keyboard_ll = 13
        wm_keydown = 0x0100
        wm_syskeydown = 0x0104
        llkhf_injected = 0x10
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
        )
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            callback_type,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        def callback(code: int, wparam: int, lparam: int) -> int:
            if code >= 0 and wparam in {wm_keydown, wm_syskeydown}:
                data = ctypes.cast(
                    lparam, ctypes.POINTER(_KbdLlHookStruct)
                ).contents
                if not (data.flags & llkhf_injected):
                    event = self.record_virtual_key(int(data.vkCode))
                    if event is not None and self.consume:
                        return 1
            return user32.CallNextHookEx(self._hook, code, wparam, lparam)

        self._callback = callback_type(callback)
        self._thread_id = int(kernel32.GetCurrentThreadId())
        module = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(
            wh_keyboard_ll, self._callback, module, 0
        )
        if not self._hook:
            self.error = f"Windows не установила перехватчик кнопок, код {kernel32.GetLastError()}."
            diagnostic_event(
                self.diagnostics_source,
                "headset",
                "media_hook_failed",
                level="error",
                detail=self.error,
            )
            self._ready.set()
            return
        self._started = True
        self._ready.set()
        diagnostic_event(
            self.diagnostics_source,
            "headset",
            "media_hook_started",
            button_count=len(self.buttons),
            consume=self.consume,
        )
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            self._started = False
            diagnostic_event(
                self.diagnostics_source, "headset", "media_hook_stopped"
            )

    def wait(self, timeout: float | None = None) -> MediaButtonEvent | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if os.name != "nt" or not self._thread_id:
            return
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread_id = 0
