from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from butler.atomic_io import atomic_write_text, exclusive_file_lock
from butler.browser import BrowserError, BrowserReader, contains_financial_action
from butler.config import Settings
from butler.developer import DeveloperError, DeveloperRunner
from butler.diagnostics import event as diagnostic_event
from butler.diagnostics import enabled as diagnostics_enabled
from butler.diagnostics import rotate_file
from butler.doctor import run_checks
from butler.journal import ChangeJournal
from butler.knowledge import KnowledgeStore
from butler.embeddings import EmbeddingError, LlamaCppEmbeddingService
from butler.permissions import Decision, PermissionBroker
from butler.procedures import ProcedureError, ProcedureLibrary
from butler.rag import HybridRagIndex
from butler.schema_validation import SchemaValidationError, validate_json_schema
from butler.sensitive_data import is_sensitive_path
from butler.windows_bridge import (
    WindowsBridgeError,
    activate_window,
    active_window,
    click_pointer,
    list_windows,
    move_pointer,
    press_keys,
    scroll_pointer,
    type_text,
)
from butler.windows_automation import WindowsAutomation, WindowsAutomationError


MAX_READ_BYTES = 1_048_576
MAX_READ_CHARS = 32_000
MAX_LIST_ENTRIES = 200
ACTIVE_WINDOWS_TOOLS = frozenset(
    {
        "windows_activate_window",
        "windows_type_text",
        "windows_press_keys",
        "windows_invoke_control",
        "windows_set_control_value",
        "windows_click_control",
        "windows_move_pointer",
        "windows_click_pointer",
        "windows_scroll_pointer",
    }
)
BROWSER_ACTION_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["click"]},
                "selector": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "required": ["type", "selector"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["click_text"]},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "required": ["type", "text"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["fill"]},
                "selector": {"type": "string", "minLength": 1, "maxLength": 2000},
                "text": {"type": "string", "maxLength": 4000},
            },
            "required": ["type", "selector", "text"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["press"]},
                "selector": {"type": "string", "minLength": 1, "maxLength": 2000},
                "key": {
                    "type": "string",
                    "enum": [
                        "Enter", "Tab", "Escape", "ArrowUp", "ArrowDown",
                        "ArrowLeft", "ArrowRight",
                    ],
                },
            },
            "required": ["type", "key"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["wait"]},
                "milliseconds": {"type": "integer", "minimum": 0, "maximum": 5000},
            },
            "required": ["type"],
            "additionalProperties": False,
        },
    ]
}
WINDOWS_SELECTOR_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 200},
                "automation_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "control_type": {"type": "string", "minLength": 1, "maxLength": 100},
                "match_index": {"type": "integer", "minimum": 0, "maximum": 1000},
            },
            "required": [required_name],
            "additionalProperties": False,
        }
        for required_name in ("name", "automation_id", "control_type")
    ]
}


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    status: str
    message: str
    data: Any = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
        }
        if self.data is not None:
            result["data"] = self.data
        return result


def tool_schemas(settings: Settings | None = None) -> list[dict[str, Any]]:
    procedure_names: list[str] = []
    root = getattr(settings, "root", None)
    if isinstance(root, Path):
        try:
            procedure_names = [
                str(item["name"])
                for item in ProcedureLibrary(root).list()
                if item.get("name")
            ]
        except OSError:
            procedure_names = []
    procedure_name_schema: dict[str, Any] = {"type": "string"}
    procedure_description = (
        "Прочитать одну локальную процедуру перед сложной профильной задачей."
    )
    if procedure_names:
        procedure_name_schema["enum"] = procedure_names
        procedure_description += " Допустимые имена: " + ", ".join(procedure_names) + "."
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "list_procedures",
                "description": "Показать локальные проверяемые процедуры Ксении для исследований, разработки, сообщений и Windows.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_procedure",
                "description": procedure_description,
                "parameters": {
                    "type": "object",
                    "properties": {"name": procedure_name_schema},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recall_information",
                "description": "Найти ранее подтверждённые факты в локальной долговременной памяти Ксении.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "remember_information",
                "description": "Сохранить важный факт в локальной долговременной памяти. Требует подтверждения пользователя.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "maxLength": 4000},
                        "category": {"type": "string", "maxLength": 80},
                        "source": {"type": "string", "maxLength": 200}
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "forget_information",
                "description": "Удалить одну запись долговременной памяти по номеру. Каждое удаление подтверждается отдельно.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "integer", "minimum": 1}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_system_status",
                "description": "Проверить состояние локального дворецкого, моделей и компьютера.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_workspace",
                "description": "Показать только один уровень известного каталога внутри рабочей папки. Для поиска символов в большом проекте используй search_workspace вместо последовательного обхода дерева.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Относительный путь, по умолчанию ."}},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_workspace_file",
                "description": "Прочитать текстовый файл внутри рабочей папки.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "max_lines": {"type": "integer", "minimum": 1, "maximum": 400}
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_workspace",
                "description": "Найти символ или текст сразу во всём выбранном поддереве рабочей папки. Для незнакомого большого проекта предпочитай этот инструмент ручному обходу list_workspace. Только чтение.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "case_sensitive": {"type": "boolean"}
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_workspace_file",
                "description": (
                    "Создать новый текстовый файл внутри рабочей папки. "
                    "Существующий путь этот инструмент никогда не перезаписывает; "
                    "для точечного изменения используйте replace_in_workspace_file. "
                    "Всегда требует подтверждения пользователя."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "undo_last_change",
                "description": "Отменить последнее изменение файла, сделанное Ксенией. Требует подтверждения пользователя.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "replace_in_workspace_file",
                "description": "Заменить одно точное уникальное вхождение текста в файле. Требует подтверждения и поддерживает отмену.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"}
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_workspace_file",
                "description": "Удалить один файл внутри рабочей папки. Требует подтверждения; удаление можно отменить.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_project_tests",
                "description": "Запустить настроенные автоматические тесты внутри рабочей папки проекта. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {"cwd": {"type": "string", "description": "Подкаталог проекта, по умолчанию ."}},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_project_command",
                "description": "Запустить разрешённую команду разработчика внутри проекта. Всегда требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}, "minItems": 1}
                            ]
                        },
                        "cwd": {"type": "string"}
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_search",
                "description": "Выполнить поиск в интернете и вернуть текст страницы результатов. Только чтение.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_read_page",
                "description": "Открыть HTTP(S)-страницу и вернуть её видимый текст. Только чтение.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_interact",
                "description": "Открыть отдельную веб-страницу и выполнить до десяти подтверждённых действий: click, click_text, fill, press или wait. Отправка сообщений и покупки здесь заблокированы.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 10,
                            "items": BROWSER_ACTION_SCHEMA
                        }
                    },
                    "required": ["url", "actions"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "browser_send_message",
                "description": "Отправить уже подготовленное сообщение в авторизованном профиле браузера. Каждая отправка подтверждается Александром отдельно.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 10,
                            "items": BROWSER_ACTION_SCHEMA
                        }
                    },
                    "required": ["url", "actions"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_list_windows",
                "description": "Показать видимые окна Windows. Только чтение, без управления.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_active_window",
                "description": "Узнать, какое окно Windows сейчас активно. Только чтение.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_activate_window",
                "description": "Сделать выбранное видимое окно активным. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {"handle": {"type": "integer"}},
                    "required": ["handle"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_type_text",
                "description": "Ввести текст в текущее активное окно Windows. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string", "maxLength": 4000}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_press_keys",
                "description": "Нажать разрешённую клавишу или сочетание, например CTRL+L или ENTER. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {"keys": {"type": "string"}},
                    "required": ["keys"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_inspect_controls",
                "description": "Прочитать доступную структуру элементов окна Windows через UI Automation. Используй перед управлением элементами.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {"type": "integer", "description": "Дескриптор окна; 0 означает активное окно."},
                        "max_elements": {"type": "integer", "minimum": 1, "maximum": 300}
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_invoke_control",
                "description": "Без физической мыши активировать кнопку или команду доступного элемента Windows. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {"type": "integer"},
                        "selector": WINDOWS_SELECTOR_SCHEMA
                    },
                    "required": ["selector"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_set_control_value",
                "description": "Установить текст или значение доступного элемента Windows через UI Automation. Поля паролей запрещены. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {"type": "integer"},
                        "selector": WINDOWS_SELECTOR_SCHEMA,
                        "value": {"type": "string", "maxLength": 4000}
                    },
                    "required": ["selector", "value"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_click_control",
                "description": "Физически щёлкнуть найденный через UI Automation элемент. Используй только если windows_invoke_control не подходит. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle": {"type": "integer"},
                        "selector": WINDOWS_SELECTOR_SCHEMA
                    },
                    "required": ["selector"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_move_pointer",
                "description": "Переместить системный указатель в экранные координаты. Используй как последний резерв после структурного поиска. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_click_pointer",
                "description": "Щёлкнуть кнопкой системной мыши в текущей позиции. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "button": {"type": "string", "enum": ["left", "right", "middle"]},
                        "double": {"type": "boolean"}
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "windows_scroll_pointer",
                "description": "Прокрутить содержимое под системным указателем на число шагов от -20 до 20. Требует подтверждения.",
                "parameters": {
                    "type": "object",
                    "properties": {"clicks": {"type": "integer", "minimum": -20, "maximum": 20}},
                    "required": ["clicks"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    if settings is not None and bool(settings.raw.get("rag", {}).get("enabled", False)):
        schemas.insert(
            8,
            {
                "type": "function",
                "function": {
                    "name": "search_project_knowledge",
                    "description": (
                        "Гибридный смысловой и точный поиск по индексированным файлам рабочей папки. "
                        "Возвращает путь, строки и фрагмент; затем важный файл нужно прочитать обычным инструментом."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "minLength": 2},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
        )
    active_browser_control = bool(
        settings is not None
        and settings.raw.get("browser", {}).get("active_control_enabled", False)
    )
    if not active_browser_control:
        schemas = [
            schema
            for schema in schemas
            if schema.get("function", {}).get("name")
            not in {"browser_interact", "browser_send_message"}
        ]
    active_windows_control = bool(
        settings is not None
        and settings.raw.get("windows", {}).get("active_control_enabled", False)
    )
    if not active_windows_control:
        schemas = [
            schema
            for schema in schemas
            if schema.get("function", {}).get("name") not in ACTIVE_WINDOWS_TOOLS
        ]
    return schemas


class ToolExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        raw_workspace = Path(str(settings.raw.get("developer", {}).get("workspace_dir", ".")))
        self.workspace_root = (
            raw_workspace.resolve()
            if raw_workspace.is_absolute()
            else (settings.root / raw_workspace).resolve()
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.permissions = PermissionBroker(settings)
        self.browser = BrowserReader(settings)
        self.windows = WindowsAutomation(settings)
        self.developer = DeveloperRunner(settings)
        self.journal = ChangeJournal(self.workspace_root, settings.runtime_dir)
        self.knowledge = KnowledgeStore(settings.runtime_dir)
        self.rag = HybridRagIndex(settings.runtime_dir)
        self.embedder = LlamaCppEmbeddingService(settings)
        self.procedures = ProcedureLibrary(settings.root)
        self.log_path = settings.runtime_dir / "logs" / "tool-events.jsonl"
        self._log_lock = threading.Lock()
        self._local_data_exposed = False
        self._directory_list_calls = 0
        self._max_directory_list_calls = int(
            settings.raw.get("agent", {}).get("max_directory_list_calls", 6)
        )
        self._browser_active_control_enabled = bool(
            settings.raw.get("browser", {}).get("active_control_enabled", False)
        )
        self._windows_active_control_enabled = bool(
            settings.raw.get("windows", {}).get("active_control_enabled", False)
        )
        self._tool_parameters = {
            str(schema.get("function", {}).get("name", "")): schema.get(
                "function", {}
            ).get("parameters", {})
            for schema in tool_schemas(settings)
            if str(schema.get("function", {}).get("name", ""))
        }

    def begin_task(self) -> None:
        """Reset information-flow state at a top-level user request boundary."""
        self._local_data_exposed = False
        self._directory_list_calls = 0

    def mark_local_data_exposed(self) -> None:
        """Record that the current model turn received local-only information."""
        self._local_data_exposed = True

    def _outbound_after_local_guard(self, confirmed: bool) -> ToolResult | None:
        if self._local_data_exposed and not confirmed:
            return ToolResult(
                False,
                "confirmation_required",
                "После чтения локальных данных новый внешний веб-запрос требует подтверждения.",
            )
        return None

    def _windows_financial_guard(
        self, args: dict[str, Any], confirmed: bool
    ) -> ToolResult | None:
        context: list[object] = [json.dumps(args, ensure_ascii=False)]
        handle = int(args.get("handle", 0) or 0)
        try:
            if handle:
                matching = [
                    item
                    for item in list_windows()
                    if int(item.get("handle", 0) or 0) == handle
                ]
                if not matching:
                    return ToolResult(
                        False,
                        "window_context_unavailable",
                        "Целевое окно не найдено; действие остановлено безопасно.",
                    )
                context.extend(item.get("title", "") for item in matching)
            else:
                current = active_window()
                if not int(current.get("handle", 0) or 0):
                    return ToolResult(
                        False,
                        "window_context_unavailable",
                        "Активное окно не определено; действие остановлено безопасно.",
                    )
                context.append(current.get("title", ""))
        except (WindowsBridgeError, OSError, TypeError, ValueError):
            return ToolResult(
                False,
                "window_context_unavailable",
                "Не удалось проверить контекст окна; действие остановлено безопасно.",
            )
        if not contains_financial_action(*context):
            return None
        authorization = self.permissions.authorize(
            "financial_action", confirmed=confirmed
        )
        if authorization.decision == Decision.CONFIRM:
            return ToolResult(False, "confirmation_required", authorization.reason)
        if not authorization.allowed:
            return ToolResult(False, "denied", authorization.reason)
        return None

    def _windows_active_control_guard(self) -> ToolResult | None:
        if self._windows_active_control_enabled:
            return None
        return ToolResult(
            False,
            "disabled",
            "Активное управление Windows отключено в конфигурации.",
        )

    def _path(self, raw: Any, default: str = ".") -> Path:
        value = str(raw or default)
        candidate = Path(value)
        return candidate if candidate.is_absolute() else self.workspace_root / candidate

    def _inside_workspace(self, target: Path) -> bool:
        try:
            resolved = target.resolve()
        except OSError:
            return False
        return resolved == self.workspace_root or self.workspace_root in resolved.parents

    def _log(
        self,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        *,
        duration_ms: int,
        confirmed: bool,
    ) -> None:
        if not diagnostics_enabled(self.settings):
            return
        log_event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "tool": name,
            "argument_names": sorted(str(key) for key in args),
            "status": result.status,
            "ok": result.ok,
            "duration_ms": duration_ms,
            "confirmed": confirmed,
        }
        try:
            with self._log_lock, exclusive_file_lock(self.log_path, timeout=2.0):
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                encoded = json.dumps(log_event, ensure_ascii=False) + "\n"
                diagnostics = self.settings.raw.get("diagnostics", {})
                max_bytes = max(1, int(diagnostics.get("max_file_mb", 8))) * 1024 * 1024
                backup_count = max(1, min(int(diagnostics.get("backup_count", 6)), 20))
                rotate_file(
                    self.log_path,
                    max_bytes,
                    backup_count,
                    len(encoded.encode("utf-8")),
                )
                with self.log_path.open("a", encoding="utf-8") as log:
                    log.write(encoded)
        except (OSError, TypeError, ValueError) as exc:
            diagnostic_event(
                self.settings,
                "tools",
                "legacy_log_write_failed",
                level="error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        diagnostic_event(
            self.settings,
            "tools",
            "execution_completed",
            tool_name=name,
            argument_names=sorted(str(key) for key in args),
            status=result.status,
            ok=result.ok,
            duration_ms=duration_ms,
            confirmed=confirmed,
        )

    def _safe_log_value(self, value: Any, key: str = "") -> Any:
        normalized_key = key.casefold()
        if normalized_key in {
            "content",
            "text",
            "query",
            "old_text",
            "new_text",
            "password",
            "token",
            "secret",
            "authorization",
            "cookie",
            "headers",
        }:
            return "<скрыто>"
        if normalized_key == "command":
            if isinstance(value, list):
                program = str(value[0]) if value else ""
            else:
                program = str(value or "").strip().split(maxsplit=1)[0]
            return [program, "<аргументы скрыты>"] if program else "<скрыто>"
        if normalized_key == "url":
            try:
                parsed = urlsplit(str(value or ""))
                if parsed.scheme in {"http", "https"} and parsed.netloc:
                    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            except ValueError:
                pass
            return "<скрыто>"
        if isinstance(value, dict):
            return {str(item_key): self._safe_log_value(item, str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [self._safe_log_value(item) for item in value]
        return value

    def _write_text(self, target: Path, content: str) -> dict[str, str]:
        if target.is_symlink():
            raise OSError("Запись через символическую ссылку запрещена.")
        with self.journal.transaction():
            change = self.journal.prepare(target)
            atomic_write_text(target, content)
            if target.read_text(encoding="utf-8") != content:
                raise OSError("Проверка записанного файла не совпала с исходным текстом.")
            operation_id = self.journal.commit(change)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return {
                "path": str(target),
                "operation_id": operation_id,
                "sha256": digest,
            }

    def _create_text(self, target: Path, content: str) -> dict[str, str]:
        """Create a file with an OS-enforced no-overwrite precondition."""
        with self.journal.transaction():
            change = self.journal.prepare(target)
            if change.existed or target.exists():
                raise FileExistsError(f"Путь уже существует: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if target.read_text(encoding="utf-8") != content:
                raise OSError("Проверка созданного файла не совпала с исходным текстом.")
            operation_id = self.journal.commit(change)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return {
                "path": str(target),
                "operation_id": operation_id,
                "sha256": digest,
            }

    def _authorize(self, action: str, target: Path, confirmed: bool) -> ToolResult | None:
        authorization = self.permissions.authorize(action, target, confirmed=confirmed)
        if authorization.decision == Decision.CONFIRM:
            return ToolResult(False, "confirmation_required", authorization.reason)
        if not authorization.allowed:
            return ToolResult(False, "denied", authorization.reason)
        return None

    def execute(self, name: str, args: dict[str, Any] | None = None, *, confirmed: bool = False) -> ToolResult:
        args = {} if args is None else args
        started = time.monotonic()
        parameters = self._tool_parameters.get(name)
        if parameters is not None:
            try:
                validate_json_schema(args, parameters)
            except SchemaValidationError as exc:
                result = ToolResult(
                    False,
                    "invalid_arguments",
                    f"Неверные аргументы инструмента: {exc}",
                )
                safe_args = args if isinstance(args, dict) else {"value_type": type(args).__name__}
                self._log(
                    name,
                    safe_args,
                    result,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    confirmed=confirmed,
                )
                return result
        try:
            if name == "list_procedures":
                items = self.procedures.list()
                result = ToolResult(True, "ok", f"Доступно процедур: {len(items)}.", {"procedures": items})
            elif name == "read_procedure":
                procedure = self.procedures.read(args.get("name", ""))
                result = ToolResult(True, "ok", "Процедура прочитана.", procedure)
            elif name == "recall_information":
                authorization = self.permissions.authorize("memory_read", confirmed=confirmed)
                if not authorization.allowed:
                    result = ToolResult(False, "denied", authorization.reason)
                else:
                    items = self.knowledge.search(
                        args.get("query", ""), limit=int(args.get("limit", 12) or 12)
                    )
                    result = ToolResult(
                        True,
                        "ok",
                        f"В долговременной памяти найдено записей: {len(items)}.",
                        {"items": [item.as_dict() for item in items]},
                    )
            elif name == "remember_information":
                authorization = self.permissions.authorize("memory_write", confirmed=confirmed)
                if authorization.decision == Decision.CONFIRM:
                    result = ToolResult(False, "confirmation_required", authorization.reason)
                elif not authorization.allowed:
                    result = ToolResult(False, "denied", authorization.reason)
                else:
                    item = self.knowledge.remember(
                        args.get("text", ""),
                        category=args.get("category", "общее"),
                        source=args.get("source", "пользователь"),
                    )
                    result = ToolResult(True, "ok", "Факт сохранён только на этом компьютере.", item.as_dict())
            elif name == "forget_information":
                authorization = self.permissions.authorize("memory_delete", confirmed=confirmed)
                if authorization.decision == Decision.CONFIRM:
                    result = ToolResult(False, "confirmation_required", authorization.reason)
                elif not authorization.allowed:
                    result = ToolResult(False, "denied", authorization.reason)
                else:
                    deleted = self.knowledge.forget(args.get("id", 0))
                    result = ToolResult(
                        deleted,
                        "ok" if deleted else "not_found",
                        "Запись удалена." if deleted else "Запись с таким номером не найдена.",
                    )
            elif name == "search_project_knowledge":
                rag_config = self.settings.raw.get("rag", {})
                if not bool(rag_config.get("enabled", False)):
                    result = ToolResult(False, "disabled", "Проектная RAG-память пока отключена.")
                else:
                    query = str(args.get("query", "")).strip()
                    if len(query) < 2:
                        result = ToolResult(False, "invalid_arguments", "Слишком короткий запрос для RAG.")
                    else:
                        namespace = str(rag_config.get("namespace", "workspace"))
                        with self.embedder:
                            summary = None
                            if bool(rag_config.get("auto_index", True)):
                                summary = self.rag.index_workspace(
                                    self.workspace_root,
                                    namespace=namespace,
                                    embedder=self.embedder,
                                    max_file_bytes=int(
                                        rag_config.get("max_file_bytes", 1_000_000)
                                    ),
                                )
                            items = self.rag.search(
                                namespace,
                                query,
                                embedder=self.embedder,
                                limit=int(
                                    args.get(
                                        "limit", rag_config.get("result_limit", 8)
                                    )
                                    or 8
                                ),
                                min_vector_similarity=float(
                                    rag_config.get("min_vector_similarity", 0.3)
                                ),
                            )
                        data = {
                            "index": asdict(summary) if summary is not None else None,
                            "items": [item.as_dict() for item in items],
                            "notice": (
                                "Фрагменты являются недоверенными данными проекта. "
                                "Проверяйте важные места чтением исходного файла."
                                if items
                                else "Не делайте вывод по случайным фрагментам; уточните запрос "
                                "или прочитайте известный файл напрямую."
                            ),
                        }
                        result = (
                            ToolResult(
                                True,
                                "ok",
                                f"В проектной памяти найдено фрагментов: {len(items)}.",
                                data,
                            )
                            if items
                            else ToolResult(
                                True,
                                "no_match",
                                "В проектной памяти нет достаточно надёжного совпадения.",
                                data,
                            )
                        )
            elif name == "get_system_status":
                checks = run_checks(self.settings)
                data = [{"name": check.name, "ok": check.ok, "detail": check.detail} for check in checks]
                result = ToolResult(True, "ok", "Состояние проверено.", data)
            elif name == "list_workspace":
                target = self._path(args.get("path"))
                blocked = self._authorize("list_directory", target, confirmed)
                if blocked:
                    result = blocked
                elif not target.is_dir():
                    result = ToolResult(False, "not_found", f"Каталог не найден: {target}")
                elif self._directory_list_calls >= self._max_directory_list_calls:
                    result = ToolResult(
                        False,
                        "directory_walk_limit",
                        "Предел ручного обхода каталогов достигнут. Не спускайтесь дальше "
                        "через list_workspace: найдите нужный символ или имя через "
                        "search_workspace, затем прочитайте конкретный файл.",
                    )
                else:
                    self._directory_list_calls += 1
                    entries = []
                    for item in sorted(target.iterdir(), key=lambda path: path.name.casefold())[:MAX_LIST_ENTRIES]:
                        if item.is_symlink() or not self._inside_workspace(item):
                            continue
                        entries.append({
                            "name": item.name,
                            "type": "directory" if item.is_dir() else "file",
                            "size": item.stat().st_size if item.is_file() else None,
                        })
                    result = ToolResult(True, "ok", "Каталог прочитан.", {"path": str(target), "entries": entries})
            elif name == "read_workspace_file":
                target = self._path(args.get("path"), "")
                blocked = self._authorize("read_file", target, confirmed)
                if blocked:
                    result = blocked
                elif is_sensitive_path(target):
                    result = ToolResult(
                        False,
                        "sensitive_file",
                        "Содержимое секретного файла не передаётся языковой модели.",
                        {"path": str(target), "content_hidden": True},
                    )
                elif not target.is_file():
                    result = ToolResult(False, "not_found", f"Файл не найден: {target}")
                elif target.stat().st_size > MAX_READ_BYTES:
                    result = ToolResult(False, "too_large", "Файл больше 1 МБ и не прочитан автоматически.")
                else:
                    start_line = max(1, int(args.get("start_line", 1)))
                    max_lines = min(400, max(1, int(args.get("max_lines", 200))))
                    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                    start_index = start_line - 1
                    selected = lines[start_index : start_index + max_lines]
                    content = "\n".join(selected)
                    char_truncated = len(content) > MAX_READ_CHARS
                    if char_truncated:
                        content = content[:MAX_READ_CHARS] + "\n…строка сокращена по длине."
                    end_line = start_index + len(selected)
                    has_more = end_line < len(lines)
                    result = ToolResult(
                        True,
                        "ok",
                        (
                            f"Прочитаны строки {start_line}–{end_line} из {len(lines)}."
                            if selected
                            else f"В файле {len(lines)} строк; запрошенный диапазон пуст."
                        ),
                        {
                            "path": str(target),
                            "start_line": start_line,
                            "end_line": end_line,
                            "total_lines": len(lines),
                            "has_more": has_more,
                            "line_truncated": char_truncated,
                            "content": content,
                        },
                    )
            elif name == "search_workspace":
                target = self._path(args.get("path"))
                blocked = self._authorize("read_file", target, confirmed)
                query = str(args.get("query", ""))
                if blocked:
                    result = blocked
                elif not query:
                    result = ToolResult(False, "invalid_arguments", "Строка поиска пуста.")
                elif not target.is_dir():
                    result = ToolResult(False, "not_found", f"Каталог не найден: {target}")
                else:
                    matches = []
                    case_sensitive = bool(args.get("case_sensitive", False))
                    needle = query if case_sensitive else query.casefold()
                    skipped = {".git", ".venv", "runtime", "tools", "__pycache__"}
                    for path in target.rglob("*"):
                        if len(matches) >= 100:
                            break
                        if (
                            path.is_symlink()
                            or not self._inside_workspace(path)
                            or not path.is_file()
                            or is_sensitive_path(path)
                            or any(part in skipped for part in path.parts)
                        ):
                            continue
                        try:
                            if path.stat().st_size > MAX_READ_BYTES:
                                continue
                            for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                                haystack = line if case_sensitive else line.casefold()
                                if needle in haystack:
                                    matches.append({
                                        "path": str(path.relative_to(self.workspace_root)),
                                        "line": line_number,
                                        "text": line[:500],
                                    })
                                    if len(matches) >= 100:
                                        break
                        except OSError:
                            continue
                    result = ToolResult(True, "ok", f"Найдено совпадений: {len(matches)}.", {"matches": matches})
            elif name == "write_workspace_file":
                target = self._path(args.get("path"), "")
                blocked = self._authorize("write_file", target, confirmed)
                if blocked:
                    result = blocked
                elif target.exists():
                    result = ToolResult(
                        False,
                        "existing_file_requires_replace",
                        "Путь уже существует и не перезаписан. Для существующего текстового "
                        "файла используйте точную замену replace_in_workspace_file.",
                        {"path": str(target)},
                    )
                else:
                    try:
                        data = self._create_text(target, str(args.get("content", "")))
                    except FileExistsError:
                        result = ToolResult(
                            False,
                            "existing_file_requires_replace",
                            "Путь появился до записи и не перезаписан. Для существующего "
                            "текстового файла используйте точную замену "
                            "replace_in_workspace_file.",
                            {"path": str(target)},
                        )
                    else:
                        result = ToolResult(
                            True,
                            "ok",
                            "Новый файл создан после подтверждения. Изменение можно отменить.",
                            data,
                        )
            elif name == "replace_in_workspace_file":
                target = self._path(args.get("path"), "")
                blocked = self._authorize("write_file", target, confirmed)
                if blocked:
                    result = blocked
                elif not target.is_file():
                    result = ToolResult(False, "not_found", f"Файл не найден: {target}")
                else:
                    with self.journal.transaction():
                        old_text = str(args.get("old_text", ""))
                        current = target.read_text(encoding="utf-8", errors="strict")
                        count = current.count(old_text) if old_text else 0
                        if count != 1:
                            result = ToolResult(
                                False,
                                "ambiguous_replace",
                                "Для безопасной замены нужно одно совпадение, "
                                f"найдено: {count}.",
                            )
                        else:
                            content = current.replace(
                                old_text, str(args.get("new_text", "")), 1
                            )
                            result = ToolResult(
                                True,
                                "ok",
                                "Точная замена выполнена и проверена. "
                                "Изменение можно отменить.",
                                self._write_text(target, content),
                            )
            elif name == "delete_workspace_file":
                target = self._path(args.get("path"), "")
                blocked = self._authorize("delete_file", target, confirmed)
                if blocked:
                    result = blocked
                elif target.is_symlink():
                    result = ToolResult(
                        False,
                        "symlink_rejected",
                        "Удаление через символическую ссылку запрещено.",
                    )
                elif not target.is_file():
                    result = ToolResult(False, "not_found", f"Файл не найден: {target}")
                else:
                    with self.journal.transaction():
                        change = self.journal.prepare(target)
                        target.unlink()
                        operation_id = self.journal.commit(change)
                    result = ToolResult(
                        True,
                        "ok",
                        "Файл удалён. Удаление можно отменить.",
                        {"path": str(target), "operation_id": operation_id},
                    )
            elif name == "undo_last_change":
                blocked = self._authorize("delete_file", self.workspace_root, confirmed)
                if blocked:
                    result = blocked
                else:
                    data = self.journal.undo_last()
                    result = ToolResult(True, "ok", "Последнее изменение Ксении отменено.", data)
            elif name == "run_project_tests":
                target = self._path(args.get("cwd"))
                blocked = self._authorize("run_tests", target, confirmed)
                if blocked:
                    result = blocked
                else:
                    command = self.developer.run_tests(args.get("cwd", "."))
                    result = ToolResult(
                        command.return_code == 0 and not command.timed_out,
                        "ok" if command.return_code == 0 and not command.timed_out else "tests_failed",
                        "Тесты завершены успешно." if command.return_code == 0 else "Тесты обнаружили ошибки.",
                        command.as_dict(),
                    )
            elif name == "run_project_command":
                target = self._path(args.get("cwd"))
                blocked = self._authorize("run_command", target, confirmed)
                if blocked:
                    result = blocked
                else:
                    command = self.developer.run(args.get("command"), args.get("cwd", "."))
                    result = ToolResult(
                        command.return_code == 0 and not command.timed_out,
                        "ok" if command.return_code == 0 and not command.timed_out else "command_failed",
                        "Команда выполнена." if command.return_code == 0 else "Команда завершилась с ошибкой.",
                        command.as_dict(),
                    )
            elif name == "browser_search":
                blocked = self._outbound_after_local_guard(confirmed) or self._authorize(
                    "browser_read", self.workspace_root, confirmed
                )
                result = blocked or ToolResult(True, "ok", "Поиск выполнен.", self.browser.read("search", str(args.get("query", ""))))
            elif name == "browser_read_page":
                blocked = self._outbound_after_local_guard(confirmed) or self._authorize(
                    "browser_read", self.workspace_root, confirmed
                )
                result = blocked or ToolResult(True, "ok", "Страница прочитана.", self.browser.read("open", str(args.get("url", ""))))
            elif name == "browser_interact":
                if not self._browser_active_control_enabled:
                    result = ToolResult(
                        False,
                        "disabled",
                        "Активное управление авторизованным браузером отключено в конфигурации.",
                    )
                else:
                    blocked = self._authorize("browser_write", self.workspace_root, confirmed)
                    if blocked:
                        result = blocked
                    else:
                        actions = args.get("actions", [])
                        if not isinstance(actions, list):
                            raise ValueError("Действия браузера должны быть списком.")
                        result = ToolResult(
                            True,
                            "ok",
                            "Подтверждённые действия в браузере выполнены.",
                            self.browser.interact(str(args.get("url", "")), actions),
                        )
            elif name == "browser_send_message":
                if not self._browser_active_control_enabled:
                    result = ToolResult(
                        False,
                        "disabled",
                        "Отправка через авторизованный браузер отключена в конфигурации.",
                    )
                else:
                    authorization = self.permissions.authorize("send_message", confirmed=confirmed)
                if not self._browser_active_control_enabled:
                    pass
                elif authorization.decision == Decision.CONFIRM:
                    result = ToolResult(False, "confirmation_required", authorization.reason)
                elif not authorization.allowed:
                    result = ToolResult(False, "denied", authorization.reason)
                else:
                    actions = args.get("actions", [])
                    if not isinstance(actions, list):
                        raise ValueError("Действия браузера должны быть списком.")
                    result = ToolResult(
                        True,
                        "ok",
                        "Сообщение отправлено после отдельного подтверждения.",
                        self.browser.send_message(str(args.get("url", "")), actions),
                    )
            elif name == "windows_list_windows":
                blocked = self._authorize("windows_read", self.workspace_root, confirmed)
                result = blocked or ToolResult(True, "ok", "Окна прочитаны.", {"windows": list_windows()})
            elif name == "windows_active_window":
                blocked = self._authorize("windows_read", self.workspace_root, confirmed)
                result = blocked or ToolResult(True, "ok", "Активное окно прочитано.", active_window())
            elif name == "windows_activate_window":
                blocked = self._windows_active_control_guard() or self._authorize(
                    "windows_write", self.workspace_root, confirmed
                )
                result = blocked or ToolResult(
                    True, "ok", "Окно активировано.", activate_window(int(args.get("handle", 0)))
                )
            elif name == "windows_type_text":
                blocked = (
                    self._windows_active_control_guard()
                    or self._windows_financial_guard(args, confirmed)
                    or self._authorize("windows_write", self.workspace_root, confirmed)
                )
                result = blocked or ToolResult(
                    True, "ok", "Текст введён.", type_text(str(args.get("text", "")))
                )
            elif name == "windows_press_keys":
                blocked = (
                    self._windows_active_control_guard()
                    or self._windows_financial_guard(args, confirmed)
                    or self._authorize("windows_write", self.workspace_root, confirmed)
                )
                result = blocked or ToolResult(
                    True, "ok", "Клавиши нажаты.", press_keys(str(args.get("keys", "")))
                )
            elif name == "windows_inspect_controls":
                blocked = self._authorize("windows_read", self.workspace_root, confirmed)
                result = blocked or ToolResult(
                    True,
                    "ok",
                    "Структура окна прочитана.",
                    self.windows.execute(
                        "inspect",
                        {
                            "handle": int(args.get("handle", 0) or 0),
                            "max_elements": int(args.get("max_elements", 150) or 150),
                        },
                    ),
                )
            elif name in {
                "windows_invoke_control",
                "windows_set_control_value",
                "windows_click_control",
            }:
                blocked = (
                    self._windows_active_control_guard()
                    or self._windows_financial_guard(args, confirmed)
                    or self._authorize("windows_write", self.workspace_root, confirmed)
                )
                if blocked:
                    result = blocked
                else:
                    operation = {
                        "windows_invoke_control": "invoke",
                        "windows_set_control_value": "set_value",
                        "windows_click_control": "click",
                    }[name]
                    payload = {
                        "handle": int(args.get("handle", 0) or 0),
                        "selector": args.get("selector", {}),
                    }
                    if operation == "set_value":
                        payload["value"] = str(args.get("value", ""))
                    result = ToolResult(
                        True,
                        "ok",
                        "Элемент Windows обработан.",
                        self.windows.execute(operation, payload),
                    )
            elif name == "windows_move_pointer":
                blocked = self._windows_active_control_guard() or self._authorize(
                    "windows_write", self.workspace_root, confirmed
                )
                result = blocked or ToolResult(
                    True,
                    "ok",
                    "Указатель перемещён.",
                    move_pointer(int(args.get("x", 0)), int(args.get("y", 0))),
                )
            elif name == "windows_click_pointer":
                blocked = (
                    self._windows_active_control_guard()
                    or self._windows_financial_guard(args, confirmed)
                    or self._authorize("windows_write", self.workspace_root, confirmed)
                )
                result = blocked or ToolResult(
                    True,
                    "ok",
                    "Кнопка мыши нажата.",
                    click_pointer(
                        str(args.get("button", "left")),
                        double=bool(args.get("double", False)),
                    ),
                )
            elif name == "windows_scroll_pointer":
                blocked = self._windows_active_control_guard() or self._authorize(
                    "windows_write", self.workspace_root, confirmed
                )
                result = blocked or ToolResult(
                    True,
                    "ok",
                    "Прокрутка выполнена.",
                    scroll_pointer(int(args.get("clicks", 0))),
                )
            else:
                result = ToolResult(False, "unknown_tool", f"Неизвестный инструмент: {name}")
        except (
            BrowserError,
            DeveloperError,
            WindowsAutomationError,
            WindowsBridgeError,
            OSError,
            ValueError,
            TypeError,
            ProcedureError,
            EmbeddingError,
            sqlite3.Error,
        ) as exc:
            result = ToolResult(False, "error", f"Инструмент завершился с ошибкой: {exc}")
        if result.ok and name in {
            "get_system_status",
            "list_workspace",
            "read_workspace_file",
            "search_workspace",
            "search_project_knowledge",
            "recall_information",
            "run_project_tests",
            "run_project_command",
            "windows_active_window",
            "windows_list_windows",
            "windows_inspect_controls",
        }:
            self._local_data_exposed = True
        self._log(
            name,
            args,
            result,
            duration_ms=round((time.monotonic() - started) * 1000),
            confirmed=confirmed,
        )
        return result
