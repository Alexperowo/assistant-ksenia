from __future__ import annotations

import ctypes
import json
import sys
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _window(handle: int):
    from pywinauto import Desktop

    if handle <= 0:
        handle = int(ctypes.windll.user32.GetForegroundWindow())
    if handle <= 0:
        raise RuntimeError("Активное окно не найдено.")
    return Desktop(backend="uia").window(handle=handle).wrapper_object()


def _safe_rectangle(info) -> dict[str, int]:
    rectangle = info.rectangle
    return {
        "left": int(rectangle.left),
        "top": int(rectangle.top),
        "right": int(rectangle.right),
        "bottom": int(rectangle.bottom),
    }


def _describe(control, index: int) -> dict[str, Any]:
    info = control.element_info
    password = bool(getattr(info, "is_password", False))
    value = ""
    if not password:
        for method_name in ("get_value", "window_text"):
            method = getattr(control, method_name, None)
            if not callable(method):
                continue
            try:
                value = str(method() or "")
                break
            except Exception:
                continue
    return {
        "index": index,
        "name": str(getattr(info, "name", "") or "")[:300],
        "automation_id": str(getattr(info, "automation_id", "") or "")[:200],
        "control_type": str(getattr(info, "control_type", "") or "")[:100],
        "class_name": str(getattr(info, "class_name", "") or "")[:150],
        "enabled": bool(getattr(info, "enabled", False)),
        "visible": bool(getattr(info, "visible", False)),
        "password": password,
        "value": "<скрыто>" if password else value[:500],
        "rectangle": _safe_rectangle(info),
    }


def _matches(control, selector: dict[str, Any]) -> bool:
    info = control.element_info
    checks = {
        "name": str(getattr(info, "name", "") or ""),
        "automation_id": str(getattr(info, "automation_id", "") or ""),
        "control_type": str(getattr(info, "control_type", "") or ""),
    }
    supplied = False
    for field, actual in checks.items():
        expected = str(selector.get(field, "") or "").strip()
        if not expected:
            continue
        supplied = True
        if actual.casefold() != expected.casefold():
            return False
    return supplied


def _find(window, selector: dict[str, Any]):
    matches = [
        control
        for control in [window, *window.descendants()]
        if _matches(control, selector)
    ]
    if not matches:
        raise RuntimeError("Элемент интерфейса не найден. Сначала обновите структуру окна.")
    index = int(selector.get("match_index", 0) or 0)
    if index < 0 or index >= len(matches):
        raise RuntimeError(f"Найдено элементов: {len(matches)}, указан неверный номер.")
    return matches[index]


def _invoke(control) -> None:
    method = getattr(control, "invoke", None)
    if callable(method):
        method()
        return
    method = getattr(control, "click", None)
    if callable(method):
        method()
        return
    raise RuntimeError("Элемент не поддерживает безопасную команду активации.")


def _set_value(control, value: str) -> None:
    if bool(getattr(control.element_info, "is_password", False)):
        raise RuntimeError("Ввод пароля через модель запрещён.")
    for method_name in ("set_edit_text", "set_value"):
        method = getattr(control, method_name, None)
        if callable(method):
            method(value)
            return
    raise RuntimeError("Элемент не поддерживает установку значения через UI Automation.")


def main() -> int:
    request = json.loads(sys.stdin.read())
    if not isinstance(request, dict):
        raise ValueError("Запрос должен быть объектом.")
    operation = str(request.get("operation", ""))
    window = _window(int(request.get("handle", 0) or 0))
    if operation == "inspect":
        maximum = min(300, max(1, int(request.get("max_elements", 150))))
        controls = [window, *window.descendants()]
        result = {
            "handle": int(window.handle),
            "title": str(window.window_text() or "")[:300],
            "controls": [_describe(item, index) for index, item in enumerate(controls[:maximum])],
            "truncated": len(controls) > maximum,
            "total_controls": len(controls),
        }
    else:
        selector = request.get("selector", {})
        if not isinstance(selector, dict):
            raise ValueError("Описание элемента должно быть объектом.")
        control = _find(window, selector)
        if operation == "invoke":
            _invoke(control)
        elif operation == "set_value":
            _set_value(control, str(request.get("value", "")))
        elif operation == "click":
            control.click_input()
        elif operation == "focus":
            control.set_focus()
        else:
            raise ValueError(f"Неизвестная операция UI Automation: {operation}")
        result = {"performed": operation, "control": _describe(control, 0)}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
