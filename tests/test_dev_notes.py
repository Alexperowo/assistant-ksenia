"""Пустые результаты инструментов (тест 2026-10-12: «Синамон», «ЛитРПГ»)."""
import core


def test_empty_results_are_recognized():
    assert core.looks_empty({"ok": True, "options": []})
    assert core.looks_empty({"ok": True, "command": "search_package", "output": "(пусто)"})
    assert not core.looks_empty({"ok": True, "options": [{"title": "x"}]})
    assert not core.looks_empty({"ok": True, "chats": []})  # «никто не писал» — это ответ, а не неудача
