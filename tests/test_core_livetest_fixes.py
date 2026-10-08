"""Исправления после живого теста на JBL (2026-10-08)."""
import core


def test_stop_words_end_conversation_but_not_sentences():
    assert core.is_stop("Стоп.")
    assert core.is_stop("Ксения, стоп!")
    assert core.is_stop("Хватит")
    assert not core.is_stop("стоп, включи музыку")
    assert not core.is_stop("останови музыку")


def test_unasked_actions_are_blocked():
    assert not core.asked_for("remind_set", "можешь во ВКонтакте отправить сообщение в избранное")
    assert not core.asked_for("window_action", "можешь тогда открыть меню приложений?")
    assert core.asked_for("remind_set", "напомни через минуту выпить воды")
    assert core.asked_for("remind_set", "поставь таймер на пять минут")
    assert core.asked_for("window_action", "сверни это окно")
    assert core.asked_for("weather", "что угодно")  # без ворот — как раньше


def test_recent_user_text_takes_last_two_without_service_notes():
    ks = core.Ksenia.__new__(core.Ksenia)
    ks.history = [
        {"role": "user", "content": "напомни мне позвонить\n\n(служебно: время)"},
        {"role": "assistant", "content": "Когда?"},
        {"role": "user", "content": "через пять минут\n\n(служебно: время)"},
        {"role": "user", "content": "(служебно: пришло напоминание)"},
    ]
    t = ks.recent_user_text()
    assert t == "напомни мне позвонить через пять минут"
    assert core.asked_for("remind_set", t)


def test_latin_stop_from_gigaam():
    assert core.is_stop("Stop.") and core.is_stop("СStop.") and core.is_stop("Ксения, stop")
    assert not core.is_stop("stop the music now")


def test_english_numbers_become_digits():
    assert core.clean_for_speech("сейчас плюс thirteen, от plus four до plus eleven") == "сейчас плюс 13, от плюс 4 до плюс 11"
    assert core.clean_for_speech("twenty-five градусов") == "25 градусов"
    assert core.clean_for_speech("Someone is here") == "Someone is here"
