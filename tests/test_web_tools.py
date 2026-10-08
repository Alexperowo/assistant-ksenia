"""Веб-инструменты: подтверждение по найденному элементу, привязка к странице, Enter = отправка формы."""
import asyncio

import pytest

from tools import browser_core, confirm, web
from fake_page import FakePage


@pytest.fixture
def page(monkeypatch):
    holder = {}

    async def fake_page(purpose):
        return holder["page"]

    monkeypatch.setattr(browser_core, "page", fake_page)
    confirm.cancel()

    def make(url, elements):
        holder["page"] = FakePage(url, elements)
        web._state["url"] = url
        return holder["page"]

    yield make
    confirm.cancel()
    web._state["url"] = None


def call(name, args):
    return asyncio.run(web.call(name, args, None))


def test_harmless_click(page):
    pg = page("https://news.example/a", [{"role": "link", "text": "Читать далее", "goto": "https://news.example/b"}])
    r = call("web_click", {"text": "читать далее"})
    assert r["ok"] and pg.log == [("click", "Читать далее")] and web._state["url"] == "https://news.example/b"


def test_risky_element_found_by_harmless_text_asks_first(page):
    pg = page("https://mail.example/inbox", [{"role": "button", "text": "Ок", "label": "Окончательно удалить письма"}])
    r = call("web_click", {"text": "Ок"})
    assert r["prepared"] and pg.log == []
    assert r["speak_verbatim"] == "Нажать «Окончательно удалить письма» на сайте mail.example?"
    assert asyncio.run(confirm.take()["run"]())["ok"] and pg.log == [("click", "Окончательно удалить письма")]


def test_confirmed_click_refuses_on_another_page(page):
    pg = page("https://shop.example/item", [{"role": "button", "text": "Удалить из избранного"}])
    r = call("web_click", {"text": "Удалить из избранного"})
    assert r["prepared"]
    pg.url = "https://other.example/"  # модель успела открыть другую страницу
    res = asyncio.run(confirm.take()["run"]())
    assert res["ok"] is False and "другая" in res["error"] and pg.log == []


def test_payment_is_refused_by_element_label(page):
    pg = page("https://shop.example/checkout", [{"role": "button", "text": "Далее", "label": "Оплатить 990 ₽"}])
    r = call("web_click", {"text": "Далее"})
    assert r["ok"] is False and "оплату" in r["error"] and pg.log == [] and confirm.current() is None


def test_enter_in_message_field_needs_confirmation(page):
    pg = page("https://vk.ru/im/convo/1", [{"kind": "field", "role": "textbox", "label": "Напишите сообщение",
                                             "placeholder": "Напишите сообщение"}])
    r = call("web_type", {"field": "Напишите сообщение", "text": "привет", "enter": True})
    assert r["prepared"] and pg.log == []  # раньше: сообщение уходило без «Отправить?»
    assert "привет" in r["speak_verbatim"]
    assert asyncio.run(confirm.take()["run"]())["ok"]
    assert pg.log == [("fill", "Напишите сообщение", "привет"), ("press", "Enter")]


def test_enter_in_search_box_is_immediate(page):
    pg = page("https://ru.wikipedia.org/", [{"kind": "field", "role": "searchbox", "label": "Искать в Википедии",
                                             "placeholder": "Искать в Википедии", "search": True}])
    r = call("web_type", {"field": "Искать", "text": "Чехов", "enter": True})
    assert r["ok"] and r.get("typed") and pg.log[-1] == ("press", "Enter")


def test_typing_without_enter_is_immediate(page):
    pg = page("https://site.example/form", [{"kind": "field", "role": "textbox", "label": "Имя", "placeholder": "Имя"}])
    r = call("web_type", {"field": "Имя", "text": "Александр"})
    assert r["ok"] and pg.log == [("fill", "Имя", "Александр")]


def test_open_refuses_redirect_into_home_network(page, monkeypatch):
    pg = page("about:blank", [])

    async def goto(url):
        pg.url = "http://192.168.0.1/admin"  # публичная страница перенаправила на роутер
        return pg

    async def blank(url, **kw):
        pg.url = url

    monkeypatch.setattr(web, "_goto", goto)
    monkeypatch.setattr(web, "_public_url", lambda url: not url.startswith("http://192.168."))
    pg.goto = blank
    r = call("web_open", {"url": "https://public.example/r"})
    assert r["ok"] is False and "домашней сети" in r["error"] and pg.url == "about:blank"
