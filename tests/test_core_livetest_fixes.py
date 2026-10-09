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


def test_agent_notes(tmp_path, monkeypatch):
    assert core.agent_note("Заметка: опять оборвала фразу") == "опять оборвала фразу"
    assert core.agent_note("Ксения, заметка для агента, музыка тихая.") == "музыка тихая."
    assert core.agent_note("Замечание — долго думает") == "долго думает"
    assert core.agent_note("Хорошо, заметка агенту. Всё работает нормально.") == "Всё работает нормально."
    assert core.agent_note("Запомни, что я люблю рок") is None
    assert core.agent_note("Мне нужна заметка в блокноте") is None
    assert core.agent_note("Какая погода?") is None
    f = tmp_path / "notes.md"
    monkeypatch.setattr(core, "NOTES_FILE", str(f))
    core.save_agent_note("тест", "Привет!")
    assert "тест" in f.read_text(encoding="utf-8") and "Привет!" in f.read_text(encoding="utf-8")


def test_startup_check_once_per_boot_and_silent_when_fine(tmp_path, monkeypatch):
    import asyncio
    said = []
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setitem(core.CONFIG, "startup_check_wait_s", 1)

    async def fast_sleep(s):
        return None

    async def fake_check(name, args, session):
        return {"problems": ["наушники не подключены — включи"], "fine": []}

    async def fake_notice(t):
        said.append(t)
    monkeypatch.setattr(core.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(core.selfcheck, "call", fake_check)
    monkeypatch.setattr(core, "say_notice", fake_notice)
    asyncio.run(core.startup_check())
    assert said == []  # выключенные наушники — не проблема
    monkeypatch.setattr(core.selfcheck, "call", lambda *a: asyncio.sleep(0, {"problems": ["мозг не работает"]}))
    asyncio.run(core.startup_check())
    assert said == []  # второй раз за ту же загрузку — не проверяет
