"""Подтверждение относится только к последнему сказанному вопросу: строгое «да», неуслышанный вопрос,
неузнанный голос, одно действие за раз, долгие действия в фоне, песочница — только чтение."""
import asyncio
import json

import pytest

import core
from tools import confirm
from test_core_respond import FakeSpeaker, call, script_steps


@pytest.fixture
def ks(monkeypatch, tmp_path):
    FakeSpeaker.instances = []
    monkeypatch.setattr(core, "Speaker", FakeSpeaker)
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(core, "waiting", [])
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start, k.last_tag, k.speaker, k.session = [], 0, None, None, None
    k.lock = asyncio.Lock()
    k.last_turn_t = 0.0
    confirm.cancel()
    yield k
    confirm.cancel()


def sender(log):
    async def send():
        log.append(1)
        return {"ok": True}
    return send


@pytest.mark.parametrize("text", ["да", "Да.", "да, отправляй", "ну давай", "конечно, пожалуйста", "Ксения, да",
                                  "угу", "ок", "отправляй", "да, удаляй"])
def test_clear_yes(text):
    assert core.is_affirmative(text)


@pytest.mark.parametrize("text", ["отправь Маше", "давай заново", "да, включи музыку", "ок, понятно", "давай потом",
                                  "да ну его", "Да? А кому?", "да ладно", "нет", "да не надо", "ага, щас",
                                  "давай я сам", "да, но поменяй текст", "погоди"])
def test_not_yes(text):
    assert not core.is_affirmative(text)


def test_question_cut_off_is_cancelled(ks):
    """Вопрос прервали на полуслове (касание, «стоп») — Александр его не услышал, «да» потом ничего не решает."""
    sent = []

    async def ask_tool(name, args, session):
        res = confirm.ask("сообщение для Димы", sender(sent), question="Пишу Диме: привет. Отправить?")
        FakeSpeaker.instances[-1].cancelled = True  # перебили, пока звучал вопрос
        return res

    core_run = core.run_tool
    core.run_tool = ask_tool
    try:
        script_steps(ks, [("", [call("v1", "vk_send", '{"to":"Дима","text":"привет"}')]), ("", [])])
        asyncio.run(ks.respond("напиши Диме привет", {"_t0": 0}))
    finally:
        core.run_tool = core_run
    assert confirm.peek() is None and sent == []


def test_unrecognized_short_yes_keeps_question(ks):
    """Образец голоса записан, «да» слишком короткое, чтобы узнать голос: не выполняем, но вопрос не теряем —
    «да, отправляй» узнанным голосом решает его."""
    sent = []
    confirm.prepare("сообщение для Димы", sender(sent))
    script_steps(ks, [("Скажи чуть длиннее.", []), ("Отправила.", [])])

    async def go():
        await ks.respond("да", {"_t0": 0}, speaker={"enrolled": True, "owner": None, "confirm_ok": False})
        assert sent == [] and confirm.peek() is not None
        await ks.respond("да, отправляй", {"_t0": 0}, speaker={"enrolled": True, "owner": True, "confirm_ok": True})

    asyncio.run(go())
    assert sent == [1]


def test_second_question_in_same_turn_is_refused(ks):
    confirm.ask("сообщение для Димы", lambda: None, question="Отправить Диме?")
    res = confirm.ask("удалить файл", lambda: None, question="Удалить?")
    assert res["ok"] is False and confirm.peek()["label"] == "сообщение для Димы"


def test_long_action_runs_in_background_and_reports_later(ks):
    started, release = [], asyncio.Event()

    async def install():
        started.append(1)
        await release.wait()
        return {"ok": True}

    async def go():
        confirm.prepare("установить программу vlc", install, background=True, limit=900)
        script_steps(ks, [("Начала, скажу, когда закончу.", [])])
        await ks.respond("да", {"_t0": 0})
        assert "НАЧАЛО" in ks.history[-2]["content"] and core.waiting == []
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert started == [1] and len(core.waiting) == 1 and "выполнено" in core.waiting[0]

    asyncio.run(go())


def test_sandbox_runs_only_read_only_tools():
    assert core.sandbox_allows("weather", "{}")
    assert core.sandbox_allows("files", json.dumps({"action": "recent"}))
    assert not core.sandbox_allows("files", json.dumps({"action": "trash"}))
    assert not core.sandbox_allows("vk_send", "{}")
    assert not core.sandbox_allows("brand_new_tool", "{}")  # новый инструмент по умолчанию не исполняется


@pytest.mark.parametrize("text", ["Купить в 1 клик", "Оформить заказ", "Оплатить 1399 ₽", "Перевести 500 руб",
                                  "Buy now", "Перейти к оплате", "Пополнить баланс", "Списать 200 ₽"])
def test_money_buttons_are_always_refused(text):
    assert confirm.is_financial(text)


@pytest.mark.parametrize("text", ["Показать перевод", "Отправить", "Удалить", "Подписаться", "Перевести страницу",
                                  "Добавить в корзину"])
def test_ordinary_buttons_are_not_money(text):
    assert not confirm.is_financial(text)


def test_desktop_money_click_refused_without_question(monkeypatch):
    from tools import desktop

    async def helper(cmd, args):
        return {"needs_confirm": True, "matched": "Купить подписку", "window": "Магазин"}

    monkeypatch.setattr(desktop, "_helper", helper)
    r = asyncio.run(desktop.call("ui_click", {"name": "кнопку"}, None))
    assert r["ok"] is False and confirm.peek() is None


@pytest.mark.parametrize("text,neg", [("нет", True), ("не надо", True), ("отмена", True), ("нет, спасибо", True),
                                       ("да", False), ("нормально", False)])
def test_short_negative(text, neg):
    assert core.is_negative(text) is neg


def test_expired_question_is_said_aloud(ks, monkeypatch):
    said = []

    async def notice(text, output=None):
        said.append(text)

    monkeypatch.setattr(core, "say_notice", notice)
    monkeypatch.setattr(core, "ks", ks)
    confirm.prepare("сообщение для Димы", lambda: None, ttl=-1)
    asyncio.run(core.announce_expired_question())
    assert said and "сообщение для Димы" in said[0] and confirm.peek() is None


def test_vk_send_refuses_links_and_same_name_people(monkeypatch):
    from tools import vk

    async def open_list():
        return None

    async def find(pg, who):
        return [{"name": "Дима", "peer": 1}, {"name": "Дима", "peer": 2}] if who == "Дима" else [{"name": "Мама", "peer": 3}]

    monkeypatch.setattr(vk, "_open_list", open_list)
    monkeypatch.setattr(vk, "_find", find)
    assert asyncio.run(vk.call("vk_send", {"to": "Дима", "text": "привет"}, None))["ok"] is False
    assert asyncio.run(vk.call("vk_send", {"to": "Мама", "text": "смотри bit.ly/x"}, None))["ok"] is False
    assert asyncio.run(vk.call("vk_send", {"to": "Мама", "text": "буду в пять"}, None))["prepared"]
