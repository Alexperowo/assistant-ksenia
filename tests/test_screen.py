"""Зрение: нарезка длинного текста, постобработка OCR, буфер обмена, ответ мозга про экран."""
import asyncio
import subprocess

import pytest

from tools import screen
from fakes import FakeResponse, FakeSession


def test_chunk_short_text_whole():
    assert screen._chunk("Короткий текст.") == ("Короткий текст.", "")


def test_chunk_cuts_at_sentence_end_any_punctuation():
    text = "Это вопрос номер такой? " * 100
    part, rest = screen._chunk(text)
    assert part.endswith("?") and len(part) <= screen.READ_CHUNK
    assert (part + " " + rest).split() == text.split()


def test_chunk_without_sentences_cuts_at_space_not_mid_word():
    text = "словоо " * 300  # 7 символов: граница 1500 приходится на середину слова
    part, rest = screen._chunk(text)
    assert part.endswith("словоо") and rest.startswith("словоо")
    assert len(part) <= screen.READ_CHUNK


def test_chunk_one_huge_word_is_hard_cut():
    text = "я" * 4000
    part, rest = screen._chunk(text)
    assert len(part) == screen.READ_CHUNK and part + rest == text


def test_read_more_walks_through_whole_text():
    text = " ".join(f"Предложение номер {i}." for i in range(400))
    got = [screen._verbatim(text, "экран")]
    while got[-1]["more_left"]:
        got.append(asyncio.run(screen.call("read_more", {}, None)))
    assert " ".join(g["speak_verbatim"] for g in got).split() == text.split()
    assert asyncio.run(screen.call("read_more", {}, None))["ok"] is False


def test_ocr_cleanup_joins_paragraph_lines_and_drops_junk():
    raw = "Первая строка\nвторая строка\n|\n—\n\nНовый абзац\n© \n\n\n"
    assert screen._ocr_cleanup(raw) == "Первая строка вторая строка\nНовый абзац"


def test_ocr_cleanup_removes_word_hyphenation_only():
    raw = "Это очень длин-\nное слово, а это северо-\nЗапад и 2023-\n2024 годы"
    assert screen._ocr_cleanup(raw) == "Это очень длинное слово, а это северо- Запад и 2023- 2024 годы"


def test_ocr_cleanup_keeps_short_words_and_numbers():
    assert screen._ocr_cleanup("Я\n42\nок") == "Я 42 ок"


@pytest.mark.parametrize("stdout,ok", [
    ("Обычный текст", True),
    ("\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + "�" * 50, False),
    ("Текст с одним � символом", True),
])
def test_clipboard_text_or_picture(monkeypatch, stdout, ok):
    seen = []

    def fake_run(*args, timeout=20):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(screen, "_run", fake_run)
    r = asyncio.run(screen.call("clipboard_read", {"source": "clipboard"}, None))
    assert r["ok"] is ok
    assert "--primary" not in seen[0]
    if not ok:
        assert "не текст" in r["error"]


def test_selection_uses_primary(monkeypatch):
    seen = []
    monkeypatch.setattr(screen, "_run", lambda *a, timeout=20: seen.append(a) or
                        subprocess.CompletedProcess(a, 1, stdout="", stderr=""))
    r = asyncio.run(screen.call("clipboard_read", {"source": "selection"}, None))
    assert "--primary" in seen[0] and r == {"ok": False, "error": "выделенного текста нет"}


def test_run_decodes_bad_bytes_without_crash():
    p = screen._run("printf", "\\377\\376ok")
    assert p.stdout.endswith("ok")


@pytest.fixture
def key(monkeypatch, tmp_path):
    f = tmp_path / "key"
    f.write_text("secret\n")
    monkeypatch.setattr(screen, "BRAIN_KEY_FILE", str(f))


def tiny_image():
    from PIL import Image
    return Image.new("RGB", (32, 16), "white")


def test_vision_error_status_is_readable(key):
    s = FakeSession(FakeResponse(503, body='{"error": "slot unavailable"}'))
    with pytest.raises(RuntimeError, match="503"):
        asyncio.run(screen._vision(tiny_image(), None, s))


def test_vision_sends_to_second_slot_without_thinking(key):
    s = FakeSession(FakeResponse(200, body='{"choices": [{"message": {"content": " Открыт браузер. "}}]}'))
    assert asyncio.run(screen._vision(tiny_image(), "что тут?", s)) == "Открыт браузер."
    url, kw = s.requests[0]
    body = kw["json"]
    assert body["id_slot"] == 1 and body["thinking_budget_tokens"] == 0
    assert kw["headers"]["Authorization"] == "Bearer secret"
    assert body["messages"][1]["content"][0]["text"] == "что тут?"


def test_vision_empty_answer(key):
    s = FakeSession(FakeResponse(200, body='{"choices": []}'))
    with pytest.raises(RuntimeError, match="пустой"):
        asyncio.run(screen._vision(tiny_image(), None, s))
