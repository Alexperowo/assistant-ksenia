"""Правила «от кого говорить сразу»: сохранение, когда сказать новое и когда напомнить."""
import asyncio

import pytest

from tools import confirm, memory, watch


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "RULES", str(tmp_path / "rules.json"))
    monkeypatch.setattr(watch, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(memory, "FILE", str(tmp_path / "memory.json"))


def call(args):
    return asyncio.run(watch.call("watch_rule", args, None))


def test_add_list_remove_also_in_memory():
    r = call({"action": "add", "who": "Курьер"})
    assert r["prepared"]  # голосом — только после «да» Александра
    from tools import confirm
    assert asyncio.run(confirm.take()["run"]())["ok"]
    assert [r["who"] for r in call({"action": "list"})["rules"]] == ["Курьер"]
    assert any("Курьер" in f["fact"] for f in memory._load())
    assert call({"action": "remove", "who": "курьер"})["ok"]
    assert call({"action": "list"})["rules"] == []
    assert not any("Курьер" in f["fact"] for f in memory._load())


def test_due_new_then_remind_then_stop():
    st = {}
    assert watch._due(st, "Курьер", "1|Еду", True, 0, 600, 2) == "new"
    assert watch._due(st, "Курьер", "1|Еду", True, 100, 600, 2) is None
    assert watch._due(st, "Курьер", "1|Еду", True, 700, 600, 2) == "remind"
    assert watch._due(st, "Курьер", "1|Еду", True, 1400, 600, 2) == "remind"
    assert watch._due(st, "Курьер", "1|Еду", True, 2100, 600, 2) is None  # не больше remind_max
    assert watch._due(st, "Курьер", "2|Я у двери", True, 2200, 600, 2) == "new"  # новое сообщение


def test_no_remind_when_not_asked():
    st = {}
    watch._due(st, "Мама", "1|Привет", False, 0, 600, 3)
    assert watch._due(st, "Мама", "1|Привет", False, 5000, 600, 3) is None
