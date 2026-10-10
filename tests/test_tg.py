"""Telegram: счётчики «3.8K», один чат — один раз, точное название важнее похожих, отправка — только после «да»."""
import asyncio

from tools import confirm, tg


def test_count():
    assert tg._count("3.8K") == 3800 and tg._count("21") == 21 and tg._count("") == 0


def test_uniq_by_peer():
    items = [{"peer": "1", "name": "A"}, {"peer": "1", "name": "A"}, {"peer": "2", "name": "B"}]
    assert [i["peer"] for i in tg._uniq(items)] == ["1", "2"]


def test_send_prepares_question_and_refuses_links(monkeypatch):
    async def page():
        return None

    async def find(pg, who):
        return [{"peer": "1530240486", "name": "Saved Messages"}] if who == "Избранное" else []

    monkeypatch.setattr(tg, "_page", page)
    monkeypatch.setattr(tg, "_find", find)
    confirm.cancel()
    r = asyncio.run(tg.call("tg_send", {"to": "Избранное", "text": "проверка"}, None))
    assert r["speak_verbatim"] == "Пишу в Telegram, Избранное: проверка. Отправить?" and confirm.peek()
    confirm.cancel()
    assert asyncio.run(tg.call("tg_send", {"to": "Избранное", "text": "смотри t.me/x"}, None))["ok"] is False
