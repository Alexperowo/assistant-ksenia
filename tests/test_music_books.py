"""Аудиокниги: место сохраняется в главах книги, а не в позициях плейлиста mpv."""
import asyncio
import json

import pytest

from tools import music


@pytest.fixture
def mpv(monkeypatch, tmp_path):
    """Подделка mpv: отвечает на get_property из словаря, записывает команды."""
    state = {"playlist-pos": 0, "time-pos": 0.0, "commands": []}

    async def fake_ipc(*cmd):
        state["commands"].append(cmd)
        if cmd[0] == "get_property":
            return {"data": state.get(cmd[1]), "error": "success"}
        return {"error": "success"}

    async def fake_ensure():
        pass

    monkeypatch.setattr(music, "_ipc", fake_ipc)
    monkeypatch.setattr(music, "_ensure_mpv", fake_ensure)
    monkeypatch.setattr(music, "BOOKS_FILE", str(tmp_path / "books.json"))
    monkeypatch.setattr(music, "_direct_link", lambda t: f"https://cdn.example/{t}.mp3")
    monkeypatch.setattr(music, "_ym_book_tracks", lambda album_id: ([f"ch{i}" for i in range(20)], "Мастер и Маргарита"))
    monkeypatch.setattr(music, "_book", {"id": None, "title": None, "search": [], "first": 0})
    monkeypatch.setattr(music, "_state", {**music._state, "playlist": [], "station": None})
    return state


def saved(state_file):
    return json.load(open(state_file, encoding="utf-8"))


def test_resume_twice_keeps_real_chapter(mpv):
    async def go():
        await music._play_book(7, chapter=5, seconds=120.0)  # продолжили с 6-й главы
        mpv["playlist-pos"], mpv["time-pos"] = 2, 61.0            # дослушали до 8-й главы
        await music.save_book_position()
        first = saved(music.BOOKS_FILE)["positions"]["7"]
        await music._play_book(7, first["chapter"], first["time"] - 5)  # «продолжи книгу» ещё раз
        mpv["playlist-pos"], mpv["time-pos"] = 0, 70.0
        await music.save_book_position()
        return first, saved(music.BOOKS_FILE)["positions"]["7"]

    first, second = asyncio.run(go())
    assert first["chapter"] == 7 and first["time"] == 61.0  # глава 8 (с нуля — 7), а не 2
    assert second["chapter"] == 7                            # раньше откатывалось к главе 1


def test_loadfile_starts_at_saved_second(mpv):
    asyncio.run(music._play_book(7, chapter=3, seconds=95.0))
    load = next(c for c in mpv["commands"] if c[0] == "loadfile")
    assert load[1].endswith("ch3.mp3") and load[2] == "replace" and load[-1] == "start=95.0"


def test_continue_uses_current_position_not_stale_autosave(mpv, monkeypatch):
    monkeypatch.setattr(music, "TOKEN_FILE", __file__)  # «ключ есть»

    async def go():
        await music._play_book(7, chapter=0)
        mpv["playlist-pos"], mpv["time-pos"] = 4, 300.0
        r = await music.call("audiobook", {"action": "continue"}, None)
        return r

    r = asyncio.run(go())
    assert r["ok"] and r["chapter"] == 5 and r["from_minute"] == round(295 / 60, 1)


def test_status_names_real_chapter(mpv):
    async def go():
        await music._play_book(7, chapter=5)
        await asyncio.sleep(0.05)  # дозагрузка следующих глав в фоне
        mpv["playlist-pos"] = 1
        mpv["media-title"] = "ch6.mp3"
        return await music.call("music_status", {}, None)

    st = asyncio.run(go())
    assert st["track"] == "Мастер и Маргарита, глава 7"
