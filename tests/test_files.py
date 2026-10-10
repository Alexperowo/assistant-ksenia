"""Файлы: поиск по словам названия, чтение документов, перенос и корзина — только через «да»."""
import asyncio
import os
import time
import zipfile

import pytest

from tools import confirm, files


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Documents").mkdir()
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "Договор старый.txt").write_text("скрыто")
    (tmp_path / "Downloads" / "Договор аренды 2026.txt").write_text("Текст договора. Пункт первый.")
    (tmp_path / "Downloads" / "фото_кот.jpg").write_bytes(b"\xff\xd8")
    with zipfile.ZipFile(tmp_path / "Documents" / "Письмо.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document><w:p><w:t>Привет, это письмо.</w:t></w:p>"
                                        "<w:p><w:t>Вторая строка &amp; конец.</w:t></w:p></w:document>")
    monkeypatch.setattr(files, "HOME", str(tmp_path))
    monkeypatch.setattr(files, "_dir", lambda xdg, fb: str(tmp_path / fb))
    files._state["last"] = []
    return tmp_path


def run(args):
    return asyncio.run(files.call("files", args, None))


def test_find_by_words_skips_hidden(home):
    r = run({"action": "find", "query": "договор"})
    assert [f["name"] for f in r["files"]] == ["Договор аренды 2026.txt"]


def test_find_by_kind_and_recent(home):
    assert run({"action": "find", "query": "", "kind": "фото"})["files"][0]["name"] == "фото_кот.jpg"
    names = {f["name"] for f in run({"action": "recent"})["files"]}
    assert {"Договор аренды 2026.txt", "Письмо.docx", "фото_кот.jpg"} <= names


def test_read_docx_verbatim(home):
    run({"action": "find", "query": "письмо"})
    r = run({"action": "read", "n": 1})
    assert "Привет, это письмо." in r["speak_verbatim"] and "& конец" in r["speak_verbatim"]


def test_move_and_trash_need_yes(home):
    confirm.cancel()
    run({"action": "find", "query": "договор"})
    r = run({"action": "move", "n": 1, "to": "документы"})
    assert r.get("prepared") and os.path.exists(home / "Downloads" / "Договор аренды 2026.txt")
    confirm.cancel()
    r = run({"action": "trash", "n": 1})
    assert r.get("prepared") and "корзину" in r["speak_verbatim"]
    confirm.cancel()


def test_any_word_fallback(home):
    r = run({"action": "find", "query": "Ксения договор"})
    assert [f["name"] for f in r["files"]] == ["Договор аренды 2026.txt"]


def test_find_text_on_screenshot():
    """Надпись на снимке находится по словам, центр — в пикселях снимка."""
    import shutil
    from PIL import Image, ImageDraw, ImageFont
    from tools import desktop
    if not shutil.which("tesseract"):
        pytest.skip("нет tesseract")
    im = Image.new("RGB", (1200, 400), "white")
    d = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
    except OSError:
        pytest.skip("нет шрифта")
    d.text((100, 80), "Отмена", fill="black", font=font)
    d.text((700, 250), "Сохранить файл", fill="black", font=font)
    x, y, seen = desktop._find_text(im, "сохранить файл")
    assert 700 < x < 1150 and 250 < y < 320
    assert desktop._find_text(im, "удалить") is None
