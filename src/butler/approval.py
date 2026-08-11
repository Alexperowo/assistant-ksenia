from __future__ import annotations


TASK_SCOPES = {
    "write_workspace_file": "workspace_changes",
    "replace_in_workspace_file": "workspace_changes",
    "undo_last_change": "workspace_changes",
    "run_project_command": "developer_commands",
    "windows_activate_window": "windows_control",
    "windows_type_text": "windows_control",
    "windows_press_keys": "windows_control",
    "windows_invoke_control": "windows_control",
    "windows_set_control_value": "windows_control",
    "windows_click_control": "windows_control",
    "windows_move_pointer": "windows_control",
    "windows_click_pointer": "windows_control",
    "windows_scroll_pointer": "windows_control",
    "browser_interact": "browser_control",
    "remember_information": "memory_changes",
}

ALWAYS_CONFIRM_TOOLS = {
    "delete_workspace_file",
    "install_software",
    "send_message",
    "financial_action",
    "forget_information",
    "browser_send_message",
}


def approval_scope(tool_name: str) -> str:
    return TASK_SCOPES.get(tool_name, tool_name)


def reusable_approval(tool_name: str) -> bool:
    return tool_name not in ALWAYS_CONFIRM_TOOLS


def approval_explanation(tool_name: str) -> str:
    if not reusable_approval(tool_name):
        return "Это подтверждение относится только к одному действию."
    labels = {
        "workspace_changes": "изменений файлов этой задачи",
        "developer_commands": "команд разработчика этой задачи",
        "windows_control": "управления Windows в этой задаче",
        "browser_control": "действий в браузере этой задачи",
        "memory_changes": "новых записей в локальной памяти этой задачи",
    }
    label = labels.get(approval_scope(tool_name), "аналогичных действий этой задачи")
    return f"Подтверждение будет действовать для {label} до её завершения."
