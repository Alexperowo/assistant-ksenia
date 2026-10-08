"""Рабочий стол через доступность: помощник atspi_helper на поддельном дереве, подтверждения ядром,
зависшая программа, огромное дерево, битые имена."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from tools import confirm, desktop

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_GI = os.path.join(HERE, "fake_gi")


def window(name, children, app="kate", active=True):
    states = ["showing", "enabled"] + (["active"] if active else [])
    return {"role": "application", "name": app, "children": [{"role": "frame", "name": name, "states": states,
                                                               "children": children}]}


def btn(name, role="push button"):
    return {"role": role, "name": name, "states": ["showing", "enabled"]}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Записать дерево и направить помощник (системный python3 в проде) на поддельный gi."""
    path, log = tmp_path / "tree.json", tmp_path / "actions.log"
    monkeypatch.setenv("FAKE_ATSPI_TREE", str(path))
    monkeypatch.setenv("FAKE_ATSPI_LOG", str(log))
    monkeypatch.setenv("PYTHONPATH", FAKE_GI)
    monkeypatch.setattr(desktop, "PYTHON", sys.executable)
    confirm.cancel()

    def write(apps):
        path.write_text(json.dumps(apps, ensure_ascii=True), encoding="utf-8")

    def actions():
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    yield write, actions
    confirm.cancel()


def run(coro):
    return asyncio.run(coro)


def test_plain_click(tree):
    write, actions = tree
    write([window("Документ — Kate", [btn("Сохранить"), btn("Открыть")])])
    r = run(desktop.call("ui_click", {"name": "сохранить"}, None))
    assert r["ok"] and r["clicked"] == "Сохранить"
    assert actions() == [{"clicked": "Сохранить"}]


def test_harmless_query_matching_risky_button_asks_first(tree):
    write, actions = tree
    write([window("Корзина — Dolphin", [btn("Окончательно удалить")], app="dolphin")])
    r = run(desktop.call("ui_click", {"name": "ок"}, None))  # «ок» — подстрока «Окончательно»
    assert r["prepared"] and actions() == []
    assert r["speak_verbatim"] == "Нажать «Окончательно удалить» в окне «Корзина — Dolphin»?"
    res = run(confirm.take()["run"]())
    assert res["ok"] and actions() == [{"clicked": "Окончательно удалить"}]


def test_confirmed_click_refuses_if_window_changed(tree):
    write, actions = tree
    write([window("Почта", [btn("Удалить")])])
    r = run(desktop.call("ui_click", {"name": "Удалить"}, None))
    assert r["prepared"]
    write([window("Важные документы", [btn("Удалить")])])  # Александр переключил окно, пока шёл вопрос
    res = run(confirm.take()["run"]())
    assert res["ok"] is False and "другое окно" in res["error"] and actions() == []


def test_confirmed_click_uses_exact_button(tree):
    write, actions = tree
    write([window("Почта", [btn("Удалить черновик"), btn("Удалить")])])
    r = run(desktop.call("ui_click", {"name": "Удалить"}, None))
    run(confirm.take()["run"]())
    assert actions() == [{"clicked": "Удалить"}]


def test_no_typing_into_terminal(tree):
    write, actions = tree
    write([window("~ : bash — Konsole", [{"role": "terminal", "name": "", "states": ["showing", "enabled", "focused"],
                                          "editable_iface": True, "text": ""}], app="konsole")])
    r = run(desktop.call("ui_type", {"text": "rm -rf ~"}, None))
    assert r["ok"] is False and "терминал" in r["error"] and actions() == []


def test_typing_into_field(tree):
    write, actions = tree
    write([window("Поиск", [{"role": "entry", "name": "Найти", "states": ["showing", "enabled", "editable"],
                             "editable_iface": True, "text": ""}])])
    r = run(desktop.call("ui_type", {"field": "найти", "text": "погода"}, None))
    assert r["ok"] and actions() == [{"typed": "погода"}]


def test_broken_names_do_not_break_the_list(tree):
    write, _ = tree
    write([window("Окно \ud800", [btn("Кнопка \udcff"), btn("Обычная")])])
    r = run(desktop.call("ui_elements", {}, None))
    assert r["ok"] and [e["name"] for e in r["elements"]][-1] == "Обычная"


def test_huge_tree_is_capped(tree):
    write, _ = tree
    write([window("Таблица", [{"role": "table", "name": "t", "generate": 200000}])])
    r = run(desktop.call("ui_elements", {}, None))
    assert r["ok"] and len(r["elements"]) == 80


def test_no_active_window(tree):
    write, _ = tree
    write([window("Фон", [btn("A")], active=False)])
    r = run(desktop.call("ui_click", {"name": "A"}, None))
    assert r["ok"] is False and "активного окна" in r["error"]


def test_hung_application_times_out(tree, monkeypatch):
    write, _ = tree
    write([{"role": "application", "name": "зависла", "hang": True, "children": []}])
    monkeypatch.setattr(desktop, "HELPER_TIMEOUT_S", 1)
    r = run(desktop.call("ui_elements", {}, None))
    assert r["ok"] is False and "зависла" in r["error"]
