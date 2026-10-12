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
    assert r["speak_verbatim"] == "В Телеграм, Избранное: проверка. Отправляю?" and confirm.peek()
    confirm.cancel()
    assert asyncio.run(tg.call("tg_send", {"to": "Избранное", "text": "смотри t.me/x"}, None))["ok"] is False


def test_no_vpn_is_explained(monkeypatch):
    import asyncio
    from tools import tg

    async def boom():
        raise Exception("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://web.telegram.org/k/")
    monkeypatch.setattr(tg, "_page", boom)
    r = asyncio.run(tg.call("tg_unread", {}, None))
    assert not r["ok"] and "VPN" in r["error"]


def test_same_name_chats_offer_candidates_and_which_picks(monkeypatch):
    """2026-10-12: две «Дамиры» — раньше только «уточни, какой», и мозг выдумывал варианты."""
    import asyncio
    from tools import confirm, tg
    items = [{"peer": "-100", "name": "Дамира", "unread": "", "muted": True, "preview": "Группа: встреча в пятницу"},
             {"peer": "555", "name": "Дамира", "unread": "", "muted": False, "preview": "Ок, до завтра"}]

    async def page():
        class P:
            async def evaluate(self, js, *a):
                return items
        return P()
    monkeypatch.setattr(tg, "_page", page)
    r = asyncio.run(tg.call("tg_send", {"to": "Дамира", "text": "привет"}, None))
    assert not r["ok"] and [c["kind"] for c in r["candidates"]] == ["группа или канал", "личный чат"]
    assert "личный один" in r["note"]
    r = asyncio.run(tg.call("tg_send", {"to": "Дамира", "text": "привет", "which": 2}, None))
    assert r["prepared"] and "личный чат" in r["speak_verbatim"]
    confirm.cancel("тест")
