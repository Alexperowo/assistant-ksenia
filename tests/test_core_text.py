"""Чистые функции ядра: разбиение на фразы, очистка для голоса."""
import core


def test_first_sentence_split_when_followed_by_text():
    sent, rest = core.split_first_sentence("Включаю радио для тебя. Сейчас")
    assert sent == "Включаю радио для тебя."
    assert rest == "Сейчас"


def test_first_sentence_waits_at_end_of_buffer():
    # точка в самом конце буфера: может оказаться «3.» перед «14» — ждём продолжения
    assert core.split_first_sentence("Включаю радио для тебя.") == (None, "Включаю радио для тебя.")


def test_first_sentence_too_short_is_not_split():
    assert core.split_first_sentence("Ага. Ну")[0] is None


def test_first_sentence_newline_counts_as_end():
    sent, rest = core.split_first_sentence("Вот что я нашла\nдальше")
    assert sent == "Вот что я нашла" and rest == "дальше"


def test_first_sentence_number_is_not_end():
    assert core.split_first_sentence("Сейчас на улице 3.5 градуса")[0] is None


def test_first_sentence_quote_after_punctuation():
    sent, rest = core.split_first_sentence("Она сказала «привет!» и ушла")
    assert sent == "Она сказала «привет!»"


def test_clean_keeps_allowed_tags_only():
    assert core.clean_for_speech("[laughing] Ну ты даёшь! [smiles] Ладно.") == "[laughing] Ну ты даёшь! Ладно."
    assert core.clean_for_speech("[Warm] Привет") == "[warm] Привет"


def test_clean_strips_markdown_emoji_urls_bullets():
    text = "**Важно**: смотри https://example.com 😀\n- первый\n• второй\n# Заголовок"
    assert core.clean_for_speech(text) == "Важно: смотри первый второй Заголовок"


def test_clean_markdown_link_keeps_text():
    assert core.clean_for_speech("Смотри [вот здесь](https://example.com/a?b=1) подробнее") == "Смотри вот здесь подробнее"


def test_clean_verbatim_keeps_bracketed_words():
    assert core.clean_for_speech("[Глава 1] Начало [сноска]", verbatim=True) == "Глава 1 Начало сноска"


def test_clean_verbatim_text_cannot_inject_voice_tags():
    out = core.clean_for_speech("Он сказал [laughing] и [whisper] ушёл", verbatim=True)
    assert "[" not in out and out == "Он сказал laughing и whisper ушёл"


def test_clean_verbatim_marks_links():
    assert core.clean_for_speech("Адрес: https://example.com/x.", verbatim=True) == "Адрес: ссылка"


def test_leaked_thinking_is_never_spoken():
    from core import clean_for_speech, strip_thinking
    t = "Разделы: Главная.<think>Conclude reasoning immediately and output the final answer now.</think> Готово."
    assert "Conclude" not in clean_for_speech(t)
    assert "Conclude" not in strip_thinking("ответ <think>Conclude reasoning")
    assert strip_thinking("[warm] Привет") == "[warm] Привет"
