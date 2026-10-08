"""Радио: фильтр станций и недоверенные данные radio-browser."""
import asyncio

import pytest

from tools import music


def st(name, url="https://stream.example/a", tags=""):
    return {"name": name, "url_resolved": url, "tags": tags, "country": "Russia"}


def test_only_http_streams():
    raw = [st("A", "file:///etc/passwd"), st("B", "av://pulse"), st("C", "lavfi://sine"), st("D", ""),
           st("E", "HTTPS://Radio.example/x"), st("F", "http://radio.example/y"), {"name": "G"}, "мусор", None]
    assert [s["name"] for s in music._safe_stations(raw)] == ["E", "F"]


def test_names_are_one_short_line():
    raw = [st("  Супер\n\nРадио  \t" + "я" * 200), {"url_resolved": "https://x.example", "name": None}]
    res = music._safe_stations(raw)
    assert "\n" not in res[0]["name"] and len(res[0]["name"]) <= 80 and res[0]["name"].startswith("Супер Радио")
    assert res[1]["name"] == "станция без названия" and res[1]["tags"] == "" and res[1]["country"] == ""


def test_not_a_list():
    assert music._safe_stations({"error": "rate limit"}) == []
    assert music._safe_stations(None) == []


def test_music_only_drops_talk_unless_asked():
    res = [st("News 1", tags="news,talk"), st("Rock", tags="rock"), st("Talk", tags="Talk Radio")]
    assert [s["name"] for s in music._music_only(res, "rock")] == ["Rock"]
    assert music._music_only(res, "news") == res
    assert music._music_only(res, "новости") == res


def test_music_only_keeps_talk_if_nothing_else():
    res = [st("News 1", tags="news")]
    assert music._music_only(res, "jazz") == res


class FakeGet:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        page = self.pages.pop(0)

        class R:
            async def json(self, **kw):
                return page

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        return R()


def test_search_tries_next_query_when_all_filtered():
    s = FakeGet([[st("Bad", "file:///x")], [st("Good")]])
    res = asyncio.run(music._search(s, "Jazz", russian=True))
    assert [x["name"] for x in res] == ["Good"]
    assert "countrycode=RU" in s.urls[0] and "tag=jazz" in s.urls[0]


@pytest.fixture
def no_mpv(monkeypatch):
    calls = []

    async def fake_ipc(*cmd):
        calls.append(cmd)
        return {"error": "success"}

    async def nothing():
        pass

    monkeypatch.setattr(music, "_ipc", fake_ipc)
    monkeypatch.setattr(music, "_ensure_mpv", nothing)
    monkeypatch.setattr(music, "_state", {**music._state, "playlist": [], "station": None, "last_results": []})
    return calls


def test_play_and_next_station(no_mpv, monkeypatch):
    stations = [st("Rock 1", "https://r1.example"), st("Rock 2", "https://r2.example")]

    async def fake_search(session, q, russian):
        return stations

    monkeypatch.setattr(music, "_search", fake_search)
    monkeypatch.setattr(music.random, "choice", lambda seq: seq[0])

    async def go():
        a = await music.call("music_play", {"query": "rock"}, None)
        b = await music.call("music_control", {"action": "next"}, None)
        return a, b

    a, b = asyncio.run(go())
    assert a["ok"] and a["station"] == "Rock 1" and b["station"] == "Rock 2"
    assert ("loadfile", "https://r2.example", "replace") in no_mpv


def test_clean_title_drops_station_junk():
    from tools import music
    assert music._clean_title('{"status":1,"message":"Ok"}\r\n0\r\n') is None
    assert music._clean_title("http://stream.example/live.mp3") is None
    assert music._clean_title("live.aac") is None
    assert music._clean_title("Чайф — Аргентина-Ямайка 5:0") == "Чайф — Аргентина-Ямайка 5:0"
    assert music._clean_title(None) is None
