"""ВКонтакте: кому готовится сообщение, что именно уйдёт, и вопрос, который слышит Александр."""
import asyncio

import pytest

from tools import browser_core, confirm, vk
from fake_page import FakePage


@pytest.mark.parametrize("query,name,ok", [
    ("Дима", "Дима Петров", True), ("Петров", "Дима Петров", True), ("Димa", "Дмитрий", False),
    ("Андрей", "Дима А.", False),        # инициал «А.» раньше совпадал с любым словом на «а»
    ("Ян", "Ян Ковальский", True), ("Ян", "Яна Смирнова", False),
    ("мама", "Мама", True), ("Сергей Петров", "Сергей Петров", True), ("", "Дима", False),
])
def test_match(query, name, ok):
    assert vk._match(query, name) is ok


@pytest.fixture
def vk_env(monkeypatch):
    confirm.cancel()
    pg = FakePage("https://vk.ru/im", [{"selector": '[data-testid="vkme_composer_input"]', "label": "composer"},
                                       {"selector": '[data-testid="vkme_composer_send"]', "label": "send"}])

    async def fake_page(purpose):
        return pg

    async def open_list():
        return pg

    async def find(p, who):
        return [{"peer": "42", "name": "Дима Петров", "unread": 0, "preview": ""}]

    async def open_convo(p, peer):
        p.log.append(("open", peer))

    async def last(p, n):
        return [{"author": "Я", "text": "привет как дела", "attachment": False}]

    monkeypatch.setattr(browser_core, "page", fake_page)
    monkeypatch.setattr(vk, "_open_list", open_list)
    monkeypatch.setattr(vk, "_find", find)
    monkeypatch.setattr(vk, "_open_convo", open_convo)
    monkeypatch.setattr(vk, "_last_messages", last)
    yield pg
    confirm.cancel()


def test_send_is_prepared_and_question_is_exact(vk_env):
    r = asyncio.run(vk.call("vk_send", {"to": "Дима", "text": "привет\nкак дела"}, None))
    assert r["prepared"] and vk_env.log == []
    # одной строкой: Enter в поле ВК отправил бы первую строку отдельно
    assert r["speak_verbatim"] == "Пишу Дима Петров: привет как дела. Отправить?"


def test_send_clears_draft_before_typing(vk_env):
    asyncio.run(vk.call("vk_send", {"to": "Дима", "text": "привет как дела"}, None))
    res = asyncio.run(confirm.take()["run"]())
    assert res["ok"]
    assert vk_env.log[:4] == [("open", "42"), ("click", "composer"), ("fill", "composer", ""), ("type", "привет как дела")]
    assert vk_env.log[-1] == ("click", "send")


def test_empty_message_is_refused(vk_env):
    r = asyncio.run(vk.call("vk_send", {"to": "Дима", "text": "  \n "}, None))
    assert r["ok"] is False and confirm.current() is None
