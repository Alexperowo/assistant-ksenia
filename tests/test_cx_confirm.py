"""Незакрытые ветки подтверждений: отказ, просрочка и повтор того же вопроса."""
import asyncio

import pytest

import core
from tools import confirm


@pytest.fixture(autouse=True)
def clean_confirmation():
    turn = confirm.TURN["n"]
    confirm.cancel()
    yield
    confirm.cancel()
    confirm.TURN["n"] = turn


def test_no_cancels_pending_action_without_running_it():
    called = []

    async def action():
        called.append(True)
        return {"ok": True}

    confirm.TURN["n"] = 40
    confirm.prepare("удалить файл", action, question="Удалить файл?")
    ks = core.Ksenia.__new__(core.Ksenia)

    note = asyncio.run(ks._resolve_confirmation("нет", 41))

    assert "НЕ выполнено" in note
    assert called == []
    assert confirm.peek() is None


def test_expired_confirmation_cannot_run_on_yes():
    called = []

    async def action():
        called.append(True)
        return {"ok": True}

    confirm.TURN["n"] = 70
    confirm.prepare("отправить сообщение", action, ttl=-1, question="Отправить?")
    ks = core.Ksenia.__new__(core.Ksenia)

    note = asyncio.run(ks._resolve_confirmation("да", 71))

    assert "устарело" in note
    assert called == []
    assert confirm.peek() is None


def test_repeated_identical_question_keeps_original_action_and_id():
    called = []

    async def first_action():
        called.append("first")
        return {"ok": True}

    async def repeated_action():
        called.append("repeated")
        return {"ok": True}

    confirm.TURN["n"] = 90
    first = confirm.ask("написать Диме", first_action, question="Диме написать «Привет»?")
    second = confirm.ask("написать Диме", repeated_action, question="Диме написать «Привет»?")

    assert first["prepared"] and second["already_asked"]
    assert second["confirm_id"] == first["confirm_id"]

    note = asyncio.run(core.Ksenia.__new__(core.Ksenia)._resolve_confirmation("да, отправляй", 91))

    assert "ВЫПОЛНИЛО" in note
    assert called == ["first"]
