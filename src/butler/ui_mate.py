from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

from butler.chat import complete_chat
from butler.config import ModelRequestMode, Settings
from butler.diagnostics import event as diagnostic_event
from butler.ui_deliberation import ActionProposal, Checkpoint


class UIMateProtocolError(RuntimeError):
    pass


_ACTIONS = {
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "drag",
    "mouse_move",
    "type",
    "hotkey",
    "press",
    "key_down",
    "key_up",
    "scroll",
    "wait",
    "call_user",
    "finished",
}
_COORDINATE_ACTIONS = {
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "drag",
    "mouse_move",
}
_KEY_ACTIONS = {"hotkey", "press", "key_down", "key_up"}


_SYSTEM_PROMPT = """You are a GUI proposal model for a local Windows assistant.
Use only what is visible in the supplied screenshot. Prefer the target application's native GUI.
Do not use a terminal, shell, scripts, macros, or a different application when the task can be
completed in the visible native GUI. Never claim an action has happened: propose exactly one next step.
Coordinates are normalized integers from 0 through 999, independent of screen resolution.
Return exactly one short <action> block followed by exactly one computer_use <tool_call> block.
No text is allowed after </tool_call>.

Allowed actions: left_click, right_click, middle_click, double_click, triple_click, drag,
mouse_move, type, hotkey, press, key_down, key_up, scroll, wait, call_user, finished.

Required XML form:
<action>Short visible next step</action>
<tool_call>
<function=computer_use>
<parameter=action>left_click</parameter>
<parameter=coordinate>[500, 500]</parameter>
</function>
</tool_call>"""


def _parse_parameter(value: str) -> Any:
    cleaned = value.strip()
    if cleaned.startswith(("[", "{")):
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
    return cleaned


def _validated_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    action = str(arguments.get("action", "")).strip().casefold()
    if action not in _ACTIONS:
        raise UIMateProtocolError(f"UI-Mate вернула неизвестное действие: {action or 'пусто'}.")
    result: dict[str, Any] = {"action": action}
    if action in _COORDINATE_ACTIONS:
        coordinate = arguments.get("coordinate")
        if not isinstance(coordinate, list) or len(coordinate) != 2:
            raise UIMateProtocolError("Координатное действие UI-Mate не содержит пару coordinate.")
        try:
            x, y = (int(coordinate[0]), int(coordinate[1]))
        except (TypeError, ValueError) as exc:
            raise UIMateProtocolError("Координаты UI-Mate должны быть целыми числами.") from exc
        if not 0 <= x <= 999 or not 0 <= y <= 999:
            raise UIMateProtocolError("Координаты UI-Mate вышли за диапазон 0–999.")
        result["coordinate"] = [x, y]
    elif action == "type":
        text = arguments.get("text")
        if not isinstance(text, str) or not text or len(text) > 4000:
            raise UIMateProtocolError("Действие type содержит пустой или слишком длинный text.")
        result["text"] = text
    elif action in _KEY_ACTIONS:
        keys = arguments.get("keys")
        if isinstance(keys, str):
            keys = [part.strip() for part in keys.split("+") if part.strip()]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 8:
            raise UIMateProtocolError("Клавиатурное действие содержит неверный список keys.")
        normalized_keys: list[str] = []
        for key in keys:
            normalized = str(key).strip().casefold()
            if not re.fullmatch(r"[a-z0-9_+ -]{1,32}", normalized):
                raise UIMateProtocolError(f"Недопустимая клавиша UI-Mate: {key}.")
            normalized_keys.append(normalized)
        result["keys"] = normalized_keys
    elif action == "scroll":
        try:
            pixels = int(arguments.get("pixels", 0))
        except (TypeError, ValueError) as exc:
            raise UIMateProtocolError("scroll.pixels должен быть целым числом.") from exc
        if not -10000 <= pixels <= 10000:
            raise UIMateProtocolError("scroll.pixels вышел за безопасный диапазон.")
        direction = str(arguments.get("direction", "vertical")).strip().casefold()
        if direction not in {"vertical", "horizontal"}:
            raise UIMateProtocolError("scroll.direction должен быть vertical или horizontal.")
        result.update(pixels=pixels, direction=direction)
    elif action == "wait":
        try:
            duration = float(arguments.get("time", 1))
        except (TypeError, ValueError) as exc:
            raise UIMateProtocolError("wait.time должен быть числом.") from exc
        if not 0.1 <= duration <= 30:
            raise UIMateProtocolError("wait.time должен быть от 0,1 до 30 секунд.")
        result["time"] = duration
    elif action == "call_user":
        text = arguments.get("text")
        if not isinstance(text, str) or not text or len(text) > 2000:
            raise UIMateProtocolError("call_user содержит пустой или слишком длинный text.")
        result["text"] = text
    elif action == "finished":
        status = str(arguments.get("status", "")).strip().casefold()
        if status not in {"success", "failure"}:
            raise UIMateProtocolError("finished.status должен быть success или failure.")
        result["status"] = status
    return result


def parse_ui_mate_response(response: str) -> ActionProposal:
    text = str(response or "").strip()
    action_blocks = re.findall(
        r"<action>\s*(.*?)\s*</action>", text, flags=re.DOTALL | re.IGNORECASE
    )
    tool_blocks = re.findall(
        r"<tool_call>\s*(.*?)\s*</tool_call>", text, flags=re.DOTALL | re.IGNORECASE
    )
    if len(action_blocks) != 1 or len(tool_blocks) != 1:
        raise UIMateProtocolError("UI-Mate должна вернуть ровно один action и один tool_call.")
    if text[text.casefold().rfind("</tool_call>") + len("</tool_call>") :].strip():
        raise UIMateProtocolError("После tool_call UI-Mate вернула лишний текст.")
    function = re.search(r"<function=([^>]+)>", tool_blocks[0], flags=re.IGNORECASE)
    if function is None or function.group(1).strip().casefold() != "computer_use":
        raise UIMateProtocolError("UI-Mate вызвала неизвестный инструмент.")
    arguments: dict[str, Any] = {}
    for match in re.finditer(
        r"<parameter=([^>]+)>\s*(.*?)\s*</parameter>",
        tool_blocks[0],
        flags=re.DOTALL | re.IGNORECASE,
    ):
        name = match.group(1).strip().casefold()
        if name in arguments:
            raise UIMateProtocolError(f"UI-Mate повторила параметр {name}.")
        arguments[name] = _parse_parameter(match.group(2))
    validated = _validated_arguments(arguments)
    description = action_blocks[0].strip()
    if not description or len(description) > 1000:
        raise UIMateProtocolError("Описание действия UI-Mate пустое или слишком длинное.")
    return ActionProposal(description, "computer_use", validated)


def _image_data_url(screenshot: bytes) -> str:
    if not isinstance(screenshot, bytes) or not screenshot or len(screenshot) > 25_000_000:
        raise UIMateProtocolError("Снимок экрана пуст или слишком велик.")
    try:
        with Image.open(BytesIO(screenshot)) as image:
            image.verify()
            mime = Image.MIME.get(image.format or "", "")
    except (UnidentifiedImageError, OSError) as exc:
        raise UIMateProtocolError("Снимок экрана имеет неподдерживаемый формат.") from exc
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise UIMateProtocolError("Для UI-Mate разрешены PNG, JPEG и WebP.")
    encoded = base64.b64encode(screenshot).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class UIMateProposer:
    """Screen-grounded proposal client. It never executes the returned action."""

    def __init__(self, settings: Settings, model_role: str | None = None) -> None:
        self.settings = settings
        configured = settings.ui_deliberation()
        selected_role = model_role or (
            configured.proposer_model if configured is not None else ""
        )
        if not selected_role:
            raise UIMateProtocolError("Модель-предлагающий для UI-Mate не настроена.")
        self.model_role = selected_role
        self.profile = settings.model(selected_role)
        self.service = settings.model_service(self.profile.service_name)

    def propose(
        self,
        task: str,
        screenshot: bytes,
        *,
        mode_name: str = "fast",
        feedback: str = "",
        checkpoint: Checkpoint | None = None,
    ) -> ActionProposal:
        mode: ModelRequestMode = self.profile.request_mode(mode_name)
        instruction = f"Task:\n{task[:8000]}"
        if feedback:
            instruction += (
                "\n\nIndependent review of the previous proposal:\n"
                f"{feedback[:2000]}\nRevise the next action accordingly."
            )
        response = complete_chat(
            self.settings,
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _image_data_url(screenshot)}},
                        {"type": "text", "text": instruction},
                    ],
                },
            ],
            checkpoint=checkpoint,
            service=self.service,
            request_mode=mode,
        )
        message = response.get("choices", [{}])[0].get("message", {})
        content = str(message.get("content") or "") if isinstance(message, dict) else ""
        proposal = parse_ui_mate_response(content)
        diagnostic_event(
            self.settings,
            "ui_mate",
            "action_proposed",
            model_role=self.model_role,
            mode=mode_name,
            action=proposal.arguments.get("action"),
        )
        return proposal
