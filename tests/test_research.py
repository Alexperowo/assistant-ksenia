"""Фоновый помощник: свои вкладки у параллельных поисков, ровно одна находка на поиск, сбой — тоже находка."""
import asyncio

import pytest

from tools import browser_core, research, web
from fakes import FakeResponse, FakeSession


class Tab:
    def __init__(self, name, log):
        self.name, self.url, self.log = name, "about:blank", log

    async def goto(self, url, **kw):
        self.url = url
        self.log.append((self.name, url))
        await asyncio.sleep(0.01)  # пока грузится — другой помощник работает

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0.01)

    async def evaluate(self, js):
        return {"title": f"title of {self.url}", "text": f"текст страницы {self.url} " * 20}


@pytest.fixture
def env(monkeypatch):
    tabs, log = {}, []

    async def page(purpose):
        return tabs.setdefault(purpose, Tab(purpose, log))

    async def search(q, news, session):
        topic = "a" if "альфа" in q else "b"
        return [{"title": f"{topic}{i}", "url": f"https://{topic}{i}.example/", "date": ""} for i in range(4)]

    monkeypatch.setattr(browser_core, "page", page)
    monkeypatch.setattr(web, "_search", search)
    monkeypatch.setattr(web, "_public_url", lambda url: url.startswith("https://"))
    monkeypatch.setattr(research, "findings", asyncio.Queue())
    monkeypatch.setattr(research, "_running", {})
    research.CTX.update({"brain_url": "http://brain", "brain_key": "k"})
    return tabs, log


def brain_answer():
    return FakeResponse(200, body='{"choices": [{"message": {"content": "Итог."}}]}')


def test_parallel_helpers_use_own_tabs(env):
    tabs, log = env
    session = FakeSession(brain_answer(), brain_answer())

    async def go():
        r1 = await research.call("research_background", {"question": "что нового про альфа"}, session)
        r2 = await research.call("research_background", {"question": "что нового про бета"}, session)
        r3 = await research.call("research_background", {"question": "третий вопрос"}, session)
        await asyncio.gather(*research._running.values())
        out = [await research.findings.get(), await research.findings.get()]
        return r1, r2, r3, out

    r1, r2, r3, out = asyncio.run(go())
    assert r1["started"] and r2["started"] and r3["ok"] is False  # третий — «помощники заняты»
    by_q = {f["question"]: f for f in out}
    assert all(s.startswith("title of https://a") for s in by_q["что нового про альфа"]["sources"])
    assert all(s.startswith("title of https://b") for s in by_q["что нового про бета"]["sources"])
    assert {name for name, _ in log} == {"research0", "research1"}
    assert research._running == {}


def test_same_question_twice_is_one_search(env):
    session = FakeSession(brain_answer())

    async def go():
        await research.call("research_background", {"question": "погода в мире"}, session)
        again = await research.call("research_background", {"question": "погода  в мире"}, session)
        await asyncio.gather(*research._running.values())
        return again, research.findings.qsize()

    again, n = asyncio.run(go())
    assert again["started"] is False and n == 1


def test_failure_still_delivers_one_finding(env, monkeypatch):
    async def broken(q, news, session):
        raise RuntimeError("bing down")

    monkeypatch.setattr(web, "_search", broken)

    async def go():
        await research.call("research_background", {"question": "что-то"}, FakeSession())
        await asyncio.gather(*research._running.values())
        return research.findings.get_nowait()

    f = asyncio.run(go())
    assert "не получился" in f["answer"] and "bing" not in f["answer"] and research._running == {}


def test_redirect_into_home_network_is_not_read(env, monkeypatch):
    tabs, log = env

    class Redirecting(Tab):
        async def goto(self, url, **kw):
            self.url = "http://192.168.0.1/"

    async def page(purpose):
        return Redirecting(purpose, log)

    monkeypatch.setattr(browser_core, "page", page)
    assert asyncio.run(research._read("https://x.example/", 0)) is None
