"""Дословное чтение: куски для голоса не длиннее предела и без потерь текста."""
import random
import re

import pytest

import core


def words(text):
    return re.findall(r"\w+", text)


def test_short_sentences_are_merged():
    parts = core.split_for_reading("Раз. Два. Три.")
    assert parts == ["Раз. Два. Три."]


def test_paragraphs_split_by_newline():
    assert core.split_for_reading("Первый абзац\n\nВторой абзац", max_len=15) == ["Первый абзац", "Второй абзац"]


def test_long_sentence_after_short_one_respects_limit():
    text = "А" * 200 + ". " + "слово " * 80
    parts = core.split_for_reading(text)
    assert max(map(len, parts)) <= 220
    assert words(" ".join(parts)) == words(text)


def test_prefers_comma_in_second_half():
    text = "x" * 150 + ", " + "у" * 30 + " " + "z " * 40
    parts = core.split_for_reading(text)
    assert parts[0].endswith(",")


def test_word_without_spaces_is_hard_cut():
    text = "a" * 500
    parts = core.split_for_reading(text)
    assert "".join(parts) == text and max(map(len, parts)) <= 220


def test_empty():
    assert core.split_for_reading("") == []
    assert core.split_for_reading(" \n\n ") == []


@pytest.mark.parametrize("seed", range(30))
def test_random_text_limit_and_no_loss(seed):
    rnd = random.Random(seed)
    vocab = ["слово", "длинноеслово", "и", "а,", "текст.", "вопрос?", "ура!", "многоточие…", "\n", "x" * 90]
    text = " ".join(rnd.choice(vocab) for _ in range(rnd.randint(1, 400)))
    max_len = rnd.choice([60, 120, 220])
    parts = core.split_for_reading(text, max_len=max_len)
    assert all(0 < len(p) <= max_len for p in parts)
    # слово длиннее предела режется посередине, поэтому сравниваем буквы подряд
    assert "".join(words(" ".join(parts))) == "".join(words(text))
