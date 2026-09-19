from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any


class WindowsBridgeError(RuntimeError):
    pass


def _hwnd_int(hwnd: Any) -> int:
    if isinstance(hwnd, int):
        return hwnd
    if hasattr(hwnd, "value") and hwnd.value is not None:
        return int(hwnd.value)
    return int(hwnd)


def _window_info(user32, hwnd: Any) -> dict[str, Any]:
    title_length = user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(title_length + 1)
    user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
    class_buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buffer, 256)
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return {
        "handle": _hwnd_int(hwnd),
        "title": title_buffer.value,
        "class_name": class_buffer.value,
        "process_id": int(process_id.value),
    }


def _attach_input_desktop(user32) -> int:
    try:
        hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
            return int(hdesk)
    except Exception:
        pass
    return 0


def list_windows() -> list[dict[str, Any]]:
    if not hasattr(ctypes, "windll"):
        raise WindowsBridgeError("Windows API недоступен.")
    user32 = ctypes.windll.user32
    hdesk = _attach_input_desktop(user32)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    result: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            info = _window_info(user32, hwnd)
            if info["title"].strip():
                result.append(info)
        return True

    if hdesk:
        user32.EnumDesktopWindows(wintypes.HANDLE(hdesk), callback, 0)
    else:
        user32.EnumWindows(callback, 0)
    return result[:100]


def active_window() -> dict[str, Any]:
    if not hasattr(ctypes, "windll"):
        raise WindowsBridgeError("Windows API недоступен.")
    user32 = ctypes.windll.user32
    _attach_input_desktop(user32)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise WindowsBridgeError("Активное окно не найдено.")
    return _window_info(user32, hwnd)


def activate_window(handle: int) -> dict[str, Any]:
    if not hasattr(ctypes, "windll"):
        raise WindowsBridgeError("Windows API недоступен.")
    user32 = ctypes.windll.user32
    hwnd = wintypes.HWND(int(handle))
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        raise WindowsBridgeError("Окно не найдено или уже закрыто.")
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    if not user32.SetForegroundWindow(hwnd):
        raise WindowsBridgeError("Windows не разрешила переключить активное окно.")
    return _window_info(user32, hwnd)


WINDOW_TITLE_ALIASES: dict[str, list[str]] = {
    "терминал": ["терминал", "terminal", "powershell", "cmd", "командная строка", "console"],
    "браузер": ["браузер", "chrome", "edge", "firefox", "opera", "yandex"],
    "блокнот": ["блокнот", "notepad"],
    "проводник": ["проводник", "explorer"],
    "ide": ["ide", "antigravity", "code", "visual studio"],
}


def find_window_by_title(query: str) -> dict[str, Any] | None:
    q = query.strip().casefold()
    if not q:
        return None
    windows = list_windows()
    for w in windows:
        if q in w["title"].casefold():
            return w
    for alias_key, keywords in WINDOW_TITLE_ALIASES.items():
        if q in alias_key or alias_key in q or any(kw in q for kw in keywords):
            for w in windows:
                wt = w["title"].casefold()
                if any(kw in wt for kw in keywords):
                    return w
    return None


SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
WM_CLOSE = 0x0010


def manage_window(handle: int = 0, action: str = "minimize", title: str = "") -> dict[str, Any]:
    if not hasattr(ctypes, "windll"):
        raise WindowsBridgeError("Windows API недоступен.")
    action_clean = action.strip().lower()
    valid_actions = {
        "minimize": "minimize",
        "свернуть": "minimize",
        "сверни": "minimize",
        "maximize": "maximize",
        "развернуть": "maximize",
        "разверни": "maximize",
        "restore": "restore",
        "восстановить": "restore",
        "восстанови": "restore",
        "close": "close",
        "закрыть": "close",
        "закрой": "close",
    }
    if action_clean not in valid_actions:
        raise WindowsBridgeError(
            f"Неподдерживаемое действие над окном: '{action}'. Доступны: minimize, maximize, restore, close."
        )
    resolved_action = valid_actions[action_clean]
    user32 = ctypes.windll.user32
    target_hwnd = None

    if title and title.strip():
        found = find_window_by_title(title)
        if found:
            target_hwnd = wintypes.HWND(int(found["handle"]))
        else:
            raise WindowsBridgeError(f"Окно с заголовком '{title}' не найдено среди видимых окон.")
    elif handle:
        target_hwnd = wintypes.HWND(int(handle))
    else:
        target_hwnd = user32.GetForegroundWindow()
        if not target_hwnd:
            raise WindowsBridgeError("Активное окно не найдено.")

    if not user32.IsWindow(target_hwnd):
        raise WindowsBridgeError("Окно не найдено или уже закрыто.")

    info = _window_info(user32, target_hwnd)

    if resolved_action == "minimize":
        user32.ShowWindow(target_hwnd, SW_MINIMIZE)
    elif resolved_action == "maximize":
        user32.ShowWindow(target_hwnd, SW_MAXIMIZE)
    elif resolved_action == "restore":
        user32.ShowWindow(target_hwnd, SW_RESTORE)
    elif resolved_action == "close":
        user32.PostMessageW(target_hwnd, WM_CLOSE, 0, 0)

    return {
        "handle": _hwnd_int(target_hwnd),
        "action": resolved_action,
        "title": info["title"],
    }



ULONG_PTR = wintypes.WPARAM
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _InputUnion(ctypes.Union):
    # INPUT is a tagged union.  Keeping only KEYBDINPUT makes cbSize too small
    # on x64 even when every emitted event is a keyboard event.
    _fields_ = [
        ("mi", _MouseInput),
        ("ki", _KeyboardInput),
        ("hi", _HardwareInput),
    ]


class _Input(ctypes.Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]


def _send_keyboard(inputs: list[_Input]) -> None:
    if not inputs:
        return
    array_type = _Input * len(inputs)
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    sent = user32.SendInput(len(inputs), array_type(*inputs), ctypes.sizeof(_Input))
    if sent != len(inputs):
        raise WindowsBridgeError("Windows приняла не все нажатия клавиатуры.")


def type_text(text: str) -> dict[str, Any]:
    if not hasattr(ctypes, "windll"):
        raise WindowsBridgeError("Windows API недоступен.")
    if not text or len(text) > 4000:
        raise WindowsBridgeError("Текст должен содержать от 1 до 4000 символов.")
    inputs: list[_Input] = []
    for character in text:
        code = ord(character)
        inputs.append(_Input(type=INPUT_KEYBOARD, ki=_KeyboardInput(0, code, KEYEVENTF_UNICODE, 0, 0)))
        inputs.append(_Input(type=INPUT_KEYBOARD, ki=_KeyboardInput(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
    _send_keyboard(inputs)
    return {"characters": len(text), "window": active_window()}


_KEYS = {
    "CTRL": 0x11,
    "SHIFT": 0x10,
    "ALT": 0x12,
    "ENTER": 0x0D,
    "ESC": 0x1B,
    "TAB": 0x09,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "SPACE": 0x20,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
}
_KEYS.update({chr(code): code for code in range(ord("A"), ord("Z") + 1)})
_KEYS.update({str(value): ord(str(value)) for value in range(10)})
_KEYS.update({f"F{value}": 0x6F + value for value in range(1, 13)})


def press_keys(keys: str) -> dict[str, Any]:
    names = [item.strip().upper() for item in keys.split("+") if item.strip()]
    if not names or len(names) > 4:
        raise WindowsBridgeError("Укажите от одной до четырёх клавиш через знак плюс.")
    try:
        codes = [_KEYS[name] for name in names]
    except KeyError as exc:
        raise WindowsBridgeError(f"Клавиша не разрешена: {exc.args[0]}") from exc
    inputs = [
        _Input(type=INPUT_KEYBOARD, ki=_KeyboardInput(code, 0, 0, 0, 0))
        for code in codes
    ]
    inputs.extend(
        _Input(type=INPUT_KEYBOARD, ki=_KeyboardInput(code, 0, KEYEVENTF_KEYUP, 0, 0))
        for code in reversed(codes)
    )
    _send_keyboard(inputs)
    return {"keys": names, "window": active_window()}


def _desktop_bounds() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    left = int(user32.GetSystemMetrics(76))
    top = int(user32.GetSystemMetrics(77))
    width = int(user32.GetSystemMetrics(78))
    height = int(user32.GetSystemMetrics(79))
    return left, top, left + width, top + height


def move_pointer(x: int, y: int) -> dict[str, Any]:
    if not hasattr(ctypes, "windll"):
        raise WindowsBridgeError("Windows API недоступен.")
    left, top, right, bottom = _desktop_bounds()
    if not left <= x < right or not top <= y < bottom:
        raise WindowsBridgeError("Координаты находятся за пределами рабочего стола.")
    if not ctypes.windll.user32.SetCursorPos(int(x), int(y)):
        raise WindowsBridgeError("Не удалось переместить указатель.")
    return {"x": int(x), "y": int(y)}


_MOUSE_BUTTONS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}


def click_pointer(button: str = "left", *, double: bool = False) -> dict[str, Any]:
    normalized = button.casefold()
    if normalized not in _MOUSE_BUTTONS:
        raise WindowsBridgeError("Разрешены левая, правая и средняя кнопки мыши.")
    down, up = _MOUSE_BUTTONS[normalized]
    repeats = 2 if double else 1
    for _ in range(repeats):
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return {"button": normalized, "double": bool(double), "x": point.x, "y": point.y}


def scroll_pointer(clicks: int) -> dict[str, Any]:
    amount = max(-20, min(20, int(clicks)))
    if amount == 0:
        raise WindowsBridgeError("Укажите ненулевое число шагов прокрутки.")
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, amount * 120, 0)
    return {"clicks": amount}
