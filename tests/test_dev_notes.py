"""«Заметка: …» — Александр диктует разработчику; ядро пишет в data/dev-notes.md (тест 2026-10-12)."""
import core


def test_note_is_saved(monkeypatch, tmp_path):
    f = tmp_path / "notes.md"
    monkeypatch.setattr(core, "DEV_NOTES", str(f))
    assert core.save_dev_note("Заметка. Живой разговор через PWA не работает, постоянные прерывания.")
    assert core.save_dev_note("Ксения, заметка: проверить связанные пакеты cinnamon")
    assert core.save_dev_note("Добавь в заметки: Дамира не нашлась в Телеграме")
    text = f.read_text(encoding="utf-8")
    assert "PWA не работает" in text and "cinnamon" in text and "Дамира" in text


def test_not_a_note(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "DEV_NOTES", str(tmp_path / "notes.md"))
    assert not core.save_dev_note("Какая погода завтра?")
    assert not core.save_dev_note("Заметка.")  # одно слово — не заметка


def test_empty_results_are_recognized():
    assert core.looks_empty({"ok": True, "options": []})
    assert core.looks_empty({"ok": True, "command": "search_package", "output": "(пусто)"})
    assert not core.looks_empty({"ok": True, "options": [{"title": "x"}]})
    assert not core.looks_empty({"ok": True, "chats": []})  # «никто не писал» — это ответ, а не неудача
