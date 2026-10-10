"""Фильтры речи: женский род о себе — по морфологии; английские числа — но не названия песен."""
import pytest

import core


@pytest.mark.parametrize("src,out", [
    ("Я понял", "Я поняла"), ("Я ошибся", "Я ошиблась"), ("Я вчера был занят", "Я вчера была занята"),
    ("я уверен, что", "я уверена, что"), ("Я тебя понял, давай", "Я тебя поняла, давай"),
    ("Я футбол люблю", "Я футбол люблю"), ("Я канал переключу", "Я канал переключу"),
    ("Я один раз видела", "Я один раз видела"), ("Он сказал: «Я понял»", "Он сказал: «Я понял»"),
])
def test_feminine(src, out):
    assert core.feminine(src) == out


@pytest.mark.parametrize("src,out", [
    ("завтра плюс thirteen градусов", "завтра плюс 13 градусов"), ("plus seventeen днём", "плюс 17 днём"),
    ("включаю Twenty One Pilots", "включаю Twenty One Pilots"), ("песня «One» Metallica", "песня «One» Metallica"),
    ("Take Five Брубека", "Take Five Брубека"), ("Nine Inch Nails", "Nine Inch Nails"),
])
def test_english_numbers(src, out):
    assert core.fix_english_numbers(src) == out


def test_grammar():
    assert core.fix_grammar("Вот что я знаю обо тебе.") == "Вот что я знаю о тебе."
    assert core.fix_grammar("обо всём") == "обо всём"


@pytest.mark.parametrize("src,out", [
    ("С самого начала фильма", "С самого начала фильма"), ("5 г. сахара", "5 граммов сахара"),
    ("в 2026 г.", "в 2026 году"), ("днём 12-14°", "днём 12-14 градусов"), ("ночью –5", "ночью минус 5"),
    ("прогноз «+14»", "прогноз «плюс 14»"),
])
def test_speech_norm_fixes(src, out):
    import speech_norm
    assert speech_norm.normalize(src) == out


@pytest.mark.parametrize("src,out", [
    ("Вот что я знаю обо тебе.", "Вот что я знаю о тебе."), ("обо этом", "об этом"), ("обо мне", "обо мне"),
    ("обо всём", "обо всём"), ("Сам могу сказать", "Сама могу сказать"), ("сам не знаю", "сама не знаю"),
    ("Ты сам видишь", "Ты сам видишь"), ("сам по себе", "сам по себе"),
])
def test_grammar_obo_sam(src, out):
    assert core.fix_grammar(src) == out
