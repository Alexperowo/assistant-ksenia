from __future__ import annotations

import shlex
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit


_ACTION_LABELS = {
    "write_workspace_file": "записать файл",
    "replace_in_workspace_file": "изменить файл",
    "delete_workspace_file": "удалить файл",
    "undo_last_change": "отменить последнее изменение файла",
    "run_project_command": "запустить команду разработчика",
    "windows_activate_window": "переключить активное окно",
    "windows_type_text": "ввести текст в активное окно",
    "windows_press_keys": "нажать клавиши",
    "windows_invoke_control": "активировать элемент Windows",
    "windows_set_control_value": "изменить значение элемента Windows",
    "windows_click_control": "щёлкнуть элемент Windows",
    "windows_move_pointer": "переместить указатель",
    "windows_click_pointer": "нажать кнопку мыши",
    "windows_scroll_pointer": "прокрутить содержимое",
    "browser_interact": "взаимодействовать с веб-страницей",
}


def _short(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_url(value: object) -> str:
    """Describe a web target without leaking query parameters or fragments."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    return _short(f"{parsed.hostname}{port}{path}")


def _program_name(command: object) -> str:
    """Return only the executable name, never command arguments."""
    if isinstance(command, list):
        first = str(command[0]) if command else ""
    else:
        first = str(command or "").strip().split(maxsplit=1)[0] if command else ""
    if not first:
        return "неизвестная программа"
    return _short(PurePath(first.strip('"\'')).name or first, 80)


def _command_parts(command: object) -> list[str]:
    if isinstance(command, list):
        return [str(item) for item in command]
    try:
        return shlex.split(str(command or ""), posix=False)
    except ValueError:
        return []


def _safe_command_detail(command: object) -> str:
    """Expose a script/module/subcommand, never arbitrary command arguments."""
    parts = _command_parts(command)
    if len(parts) < 2:
        return ""
    program = PurePath(parts[0].strip('"\'')).name.casefold()
    lowered = [item.casefold() for item in parts[1:]]
    if program in {"python", "python.exe", "py"}:
        if "-m" in lowered:
            position = lowered.index("-m") + 2
            if position < len(parts):
                return f"модуль {_short(parts[position], 80)}"
        script = next((item for item in parts[1:] if item.casefold().endswith(".py")), "")
        if script:
            return f"скрипт {_short(PurePath(script.strip(chr(34))).name, 100)}"
    if program == "node":
        script = next(
            (
                item
                for item in parts[1:]
                if item.casefold().endswith((".js", ".mjs", ".cjs"))
            ),
            "",
        )
        if script:
            return f"скрипт {_short(PurePath(script.strip(chr(34))).name, 100)}"
    if program == "git":
        subcommand = next((item for item in parts[1:] if not item.startswith("-")), "")
        if subcommand:
            return f"операция {_short(subcommand, 50)}"
    if program in {"npm", "npm.cmd"}:
        subcommand = next((item for item in parts[1:] if not item.startswith("-")), "")
        if subcommand:
            return f"операция {_short(subcommand, 50)}"
    return ""


def _window_title(handle: object | None = None) -> str:
    try:
        from butler.windows_bridge import active_window, list_windows

        if isinstance(handle, int):
            match = next(
                (item for item in list_windows() if item.get("handle") == handle), None
            )
            title = match.get("title", "") if match else ""
        else:
            title = active_window().get("title", "")
        return _short(title, 120)
    except (OSError, RuntimeError, StopIteration):
        return ""


def confirmation_text(name: str, arguments: dict[str, Any] | None) -> str:
    """Build an audible, useful confirmation without exposing sensitive payloads."""
    values = arguments if isinstance(arguments, dict) else {}
    action = _ACTION_LABELS.get(name, "выполнить защищённое действие")
    target = ""

    if name in {
        "write_workspace_file",
        "replace_in_workspace_file",
        "delete_workspace_file",
    }:
        target = _short(values.get("path"))
    elif name == "run_project_command":
        target = f"программа {_program_name(values.get('command'))}"
        detail = _safe_command_detail(values.get("command"))
        if detail:
            target += f", {detail}"
        cwd = _short(values.get("cwd"))
        if cwd:
            target += f", рабочая папка {cwd}"
    elif name == "windows_activate_window":
        handle = values.get("handle")
        title = _window_title(handle)
        target = f"окно «{title}»" if title else "выбранное окно"
    elif name == "windows_type_text":
        # The text may be a password, token or personal message. Only its length is safe.
        target = f"текст длиной {len(str(values.get('text', '')))} символов"
        title = _window_title()
        if title:
            target += f" в окно «{title}»"
    elif name == "windows_press_keys":
        keys = _short(values.get("keys"), 60)
        target = f"клавиши {keys}" if keys else "указанные клавиши"
        title = _window_title()
        if title:
            target += f" в окне «{title}»"
    elif name in {
        "windows_invoke_control",
        "windows_set_control_value",
        "windows_click_control",
    }:
        selector = values.get("selector", {})
        selector = selector if isinstance(selector, dict) else {}
        label = _short(
            selector.get("name")
            or selector.get("automation_id")
            or selector.get("control_type")
        )
        target = f"элемент «{label}»" if label else "выбранный элемент"
        title = _window_title(values.get("handle"))
        if title:
            target += f" в окне «{title}»"
    elif name == "windows_move_pointer":
        target = f"координаты {values.get('x')}, {values.get('y')}"
    elif name == "windows_click_pointer":
        target = f"кнопка {values.get('button', 'left')}"
    elif name == "windows_scroll_pointer":
        target = f"шагов прокрутки {values.get('clicks')}"
    elif name == "browser_interact":
        target = _safe_url(values.get("url"))
        actions = values.get("actions", [])
        if isinstance(actions, list) and actions:
            labels = {
                "click": "нажать элемент",
                "click_text": "нажать текст",
                "fill": "заполнить поле",
                "press": "нажать клавишу",
                "wait": "подождать",
            }
            kinds = [
                labels.get(str(item.get("type", "")), "действие")
                for item in actions
                if isinstance(item, dict)
            ]
            if kinds:
                target += f"; действий {len(kinds)}: " + ", ".join(kinds)
    elif name in {"browser_search", "browser_read_page"}:
        target = (
            "внешний веб-запрос после чтения локальных данных; "
            "содержимое запроса не озвучивается"
        )

    suffix = f" Цель: {target}." if target else ""
    return f"Ксения хочет {action}.{suffix} Подтвердить?"
