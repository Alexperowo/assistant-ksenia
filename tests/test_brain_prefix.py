"""Начало запроса к мозгу не меняется без нужды: гибридный мозг при любом изменении начала пересчитывает весь
разговор (~13 с тишины). Перезапуск ядра и пауза с новой записью дневника не должны этого вызывать."""
import asyncio
import datetime
import json
import time

import pytest

import core
import diary
from tools import memory
from test_core_respond import FakeSpeaker, script_steps


@pytest.fixture
def files(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(memory, "FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(diary, "FILE", str(tmp_path / "diary.json"))
    monkeypatch.setattr(core, "Speaker", FakeSpeaker)
    return tmp_path


def talk(n):
    out = []
    for i in range(n):
        out += [{"role": "user", "content": f"реплика {i}\n\n(служебно: время)"},
                {"role": "assistant", "content": f"ответ {i}"}]
    return out


def fresh(history):
    """Ядро после перезапуска: история из файла, подсказка собрана заново, снимок — если подходит."""
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start = history, 0
    k.build_system()
    restored = k._restore_prefix()
    return k, restored


def test_core_restart_keeps_the_same_prompt_start(files):
    k, _ = fresh(talk(40))
    k.window_start = 52  # окно когда-то прыгнуло
    k.system = core.PERSONA + "\n(подсказка, с которой мозг считал разговор)"
    k.save_history()
    # после перерыва дневник записал новый разговор: собранная «с нуля» подсказка была бы другой
    diary.save({"entries": [{"date": datetime.date.today().isoformat(), "summary": "говорили о Риме"}], "upto": 80})
    k2, restored = fresh(json.load(open(core.HISTORY_FILE, encoding="utf-8")))
    assert restored
    assert k2.system == k.system and k2.window_start == 52
    assert k2._window()[0] == k.history[52]


def test_snapshot_ignored_when_persona_or_history_changed(files, monkeypatch):
    k, _ = fresh(talk(10))
    k.window_start = 4
    k.save_history()
    hist = json.load(open(core.HISTORY_FILE, encoding="utf-8"))
    other = [dict(m) for m in hist]
    other[4]["content"] = "историю правили руками"
    assert not fresh(other)[1]
    monkeypatch.setattr(core, "PERSONA", core.PERSONA + "\nновое правило")
    assert not fresh(hist)[1]  # личность поменялась — подсказку собрать заново (один пересчёт — это нормально)


def test_window_from_end_survives_history_trim(files, monkeypatch):
    monkeypatch.setitem(core.CONFIG, "history_keep", 50)
    k, _ = fresh(talk(60))  # 120 сообщений в памяти, в файл уходят последние 50
    k.window_start = 100
    k.save_history()
    k2, restored = fresh(json.load(open(core.HISTORY_FILE, encoding="utf-8")))
    assert restored and k2.history[k2.window_start] == k.history[100]


def test_pause_with_new_diary_entry_does_not_change_the_prompt(files, monkeypatch):
    k, _ = fresh(talk(3))
    k.lock, k.speaker, k.session, k.last_tag = asyncio.Lock(), None, None, None
    system = k.system
    # дневник записал разговор, прошёл час (раньше: подсказка пересобиралась — полный пересчёт)
    diary.save({"entries": [{"date": (datetime.date.today() - datetime.timedelta(days=1)).isoformat(),
                             "summary": "Александр собирался в поездку", "followup": "как съездил?"}], "upto": 6})
    diary.changed["flag"] = True
    k.last_turn_t = time.time() - 26 * 3600  # вчера — первый разговор дня
    script_steps(k, [("Привет!", [])])
    asyncio.run(k.respond("Привет", {"_t0": time.time()}))
    assert k.system == system
    note = k.history[-2]["content"]
    assert "первый разговор за сегодня" in note and "вчера: Александр собирался в поездку" in note


def test_memory_added_elsewhere_arrives_as_a_note_once(files):
    k, _ = fresh(talk(2))
    k.lock, k.speaker, k.session, k.last_tag = asyncio.Lock(), None, None, None
    k.last_turn_t = time.time()
    system = k.system
    asyncio.run(memory._remember("любит джаз"))  # например, с планшета, из центра управления
    script_steps(k, [("Ага.", []), ("Ну да.", [])])
    asyncio.run(k.respond("Ну что?", {"_t0": time.time()}))
    assert "в памяти новое: любит джаз" in k.history[-2]["content"] and k.system == system
    asyncio.run(k.respond("А ещё?", {"_t0": time.time()}))
    assert "в памяти новое" not in k.history[-2]["content"]  # один раз


def test_window_jump_refreshes_the_prompt(files, monkeypatch):
    monkeypatch.setitem(core.CONFIG, "history_max", 10)
    k, _ = fresh(talk(4))
    asyncio.run(memory._remember("любит джаз"))
    old = k.system
    k._window()
    assert k.system == old  # окно не прыгнуло — подсказка та же
    k.history += talk(2)  # 12 > 10: прыжок, начало запроса меняется всё равно
    k._window()
    assert "любит джаз" in k.system and k.facts_seen == ["любит джаз"]
