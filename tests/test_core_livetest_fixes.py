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


def test_unsure_asr_asks_to_repeat(monkeypatch, tmp_path):
    import asyncio
    seen = []
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start, k.last_tag, k.speaker, k.session = [], 0, None, None, None
    k.lock = asyncio.Lock()
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "h.json"))

    async def fake_step(budget, queue, speaker, timings, first_step):
        seen.append(k.history[-1]["content"])
        return "Что-что?", [], False
    k._step = fake_step

    class Sp:
        cancelled, started, recorded = False, False, b""
        async def warm(self): pass
        async def speak(self, *a, **kw): pass
        async def finish(self): pass
        async def cancel(self): pass
        def progress(self): return "", ""
    monkeypatch.setattr(core, "Speaker", lambda *a, **kw: Sp())
    asyncio.run(k.respond("Замеер.", {"_t0": 0, "listen": {"asr": {"mean": -0.38, "weak": ["Замеер"]}}}))
    assert "переспроси" in seen[-1]
    asyncio.run(k.respond("Расскажи про Древний Рим.", {"_t0": 0, "listen": {"asr": {"mean": -0.04, "weak": ["Древний"]}}}))
    assert "не уверено в словах: «Древний»" in seen[-1]


def test_reminder_replayed_on_speakers_only_with_headset(monkeypatch):
    import asyncio
    import subprocess as sp
    played = []

    class R:
        def __init__(self, out):
            self.stdout = out

    class P:
        def __init__(self):
            self.stdin = self

        def write(self, b):
            played.append(len(b))

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait(self):
            return 0

    async def fake_exec(*a, **k):
        played.append(a[a.index("-d") + 1])
        return P()
    monkeypatch.setattr(core.asyncio, "create_subprocess_exec", fake_exec)
    with_headset = "1\tbluez_output.AA_BB.1\tPipeWire\n2\talsa_output.pci-0000_07_00.1.hdmi-stereo\tPipeWire\n"
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: R(with_headset))
    assert asyncio.run(core.replay_on_speakers(b"\x00\x00" * 100)) is True
    assert "alsa_output.pci-0000_07_00.1.hdmi-stereo" in played
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: R("2\talsa_output.hdmi-stereo\tPipeWire\n"))
    assert asyncio.run(core.replay_on_speakers(b"\x00\x00")) is False  # без наушников голос и так в колонках


def test_wifi_connect_needs_yes():
    import asyncio
    from tools import confirm, settings
    confirm.cancel()
    r = asyncio.run(settings.call("setting", {"action": "wifi_connect", "value": "Сосед"}, None))
    assert r.get("prepared") and "вернусь на прежнюю" in r["speak_verbatim"]
    confirm.cancel()
    assert not asyncio.run(settings.call("setting", {"action": "brightness_set", "value": "300"}, None))["ok"]
