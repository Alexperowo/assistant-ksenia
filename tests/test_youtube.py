"""YouTube: «покажи видео» открывает видео на весь экран, обычная просьба — только звук."""
import asyncio

import pytest

from tools import music


@pytest.fixture
def yt(monkeypatch):
    calls = {"exec": [], "ipc": []}
    monkeypatch.setattr(music, "_yt_find", lambda q, video=False: ("Песня", 200, "https://youtu.be/x", "https://a/x"))

    async def fake_exec(*argv, **kw):
        calls["exec"].append(argv)

        class P:
            returncode = None
        return P()

    async def fake_ipc(*a):
        calls["ipc"].append(a)

    async def nothing(*a, **k):
        pass

    monkeypatch.setattr(music.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(music, "_ipc", fake_ipc)
    monkeypatch.setattr(music, "_ensure_mpv", nothing)
    monkeypatch.setattr(music, "save_book_position", nothing)
    monkeypatch.setattr(music, "_apply_volume", nothing)
    monkeypatch.setattr(music, "_pause_for_video", nothing)
    import tools.screen as screen
    monkeypatch.setattr(screen, "_dpms_is_off", lambda: False)
    return calls


def test_video_opens_fullscreen(yt):
    r = asyncio.run(music._youtube("клип", True))
    assert r["video"] == "Песня" and yt["exec"] and "--fs" in yt["exec"][0]


def test_audio_only_by_default(yt):
    r = asyncio.run(music._youtube("песня", False))
    assert r["playing"] == "Песня" and not yt["exec"]
