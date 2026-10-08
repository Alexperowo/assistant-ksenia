"""Фоновая дозагрузка очереди: старое включение не дописывает треки в новое."""
import asyncio
import time

import pytest

from tools import music


@pytest.fixture
def mpv(monkeypatch, tmp_path):
    log = []

    async def fake_ipc(*cmd):
        log.append(cmd)
        if cmd[0] == "get_property":
            return {"data": None, "error": "success"}
        return {"error": "success"}

    async def fake_ensure():
        pass

    def slow_link(t):
        time.sleep(0.02)  # прямая ссылка Яндекса — сетевой запрос
        return f"https://cdn.example/{t}.mp3"

    monkeypatch.setattr(music, "_ipc", fake_ipc)
    monkeypatch.setattr(music, "_ensure_mpv", fake_ensure)
    monkeypatch.setattr(music, "_direct_link", slow_link)
    monkeypatch.setattr(music, "_track_name", lambda t: str(t))
    monkeypatch.setattr(music, "BOOKS_FILE", str(tmp_path / "books.json"))
    monkeypatch.setattr(music, "_book", {"id": None, "title": None, "search": [], "first": 0})
    monkeypatch.setattr(music, "_state", {**music._state, "playlist": [], "station": None})
    monkeypatch.setattr(music, "_play", {"gen": 0, "task": None})
    return log


def appended(log):
    return [c[1] for c in log if c[0] == "loadfile" and c[2] == "append"]


def test_queue_is_filled_in_background(mpv):
    async def go():
        await music._play_tracks(["a1", "a2", "a3"], "Земфира")
        await music._play["task"]

    asyncio.run(go())
    assert appended(mpv) == ["https://cdn.example/a2.mp3", "https://cdn.example/a3.mp3"]
    assert music._state["playlist"] == ["a1", "a2", "a3"]


def test_switch_to_radio_stops_old_queue(mpv):
    async def go():
        await music._play_tracks([f"a{i}" for i in range(10)], "Земфира")
        await asyncio.sleep(0.05)  # пара треков успела дописаться
        mark = len(mpv)
        await music._play_station({"name": "Jazz FM", "url_resolved": "https://radio.example/jazz"})
        await asyncio.sleep(0.3)
        return mark

    mark = asyncio.run(go())
    after = mpv[mark:]
    assert ("loadfile", "https://radio.example/jazz", "replace") in after
    assert appended(after) == []            # раньше Земфира дописывалась за радио
    assert music._state["playlist"] == []    # и «следующий» переключает станцию, а не трек


def test_new_album_replaces_old_queue(mpv):
    async def go():
        await music._play_tracks([f"a{i}" for i in range(10)], "A")
        await asyncio.sleep(0.03)
        await music._play_tracks(["b0", "b1", "b2"], "B")
        await asyncio.sleep(0.3)

    asyncio.run(go())
    assert music._state["playlist"] == ["b0", "b1", "b2"]


def test_stop_cancels_background_queue(mpv):
    async def go():
        await music._play_tracks([f"a{i}" for i in range(10)], "A")
        await music.call("music_control", {"action": "stop"}, None)
        n = len(appended(mpv))
        await asyncio.sleep(0.3)
        return n

    n = asyncio.run(go())
    assert len(appended(mpv)) == n and music._state["playlist"] == []
