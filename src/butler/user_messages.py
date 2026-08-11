from __future__ import annotations


def spoken_agent_error(error: Exception) -> str:
    """Convert internal agent failures into short actionable speech."""
    detail = str(error).casefold()
    if "уже выполняет другую задачу" in detail:
        return "Я уже выполняю другую задачу. Дождитесь сообщения готово и повторите."
    return "Не удалось завершить задачу. Повторите или запустите полный аудит."
