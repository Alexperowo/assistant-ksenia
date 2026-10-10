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


def _dictate_env(monkeypatch, window):
    from tools import desktop
    keys = []

    async def helper(cmd, args=None):
        return {"window": window["now"]}

    async def sh(*argv, input_bytes=None):
        keys.append(argv)
        return 0, ""

    monkeypatch.setattr(desktop, "_helper", helper)
    monkeypatch.setattr(desktop, "_sh", sh)
    monkeypatch.setattr(desktop.asyncio, "sleep", lambda s: asyncio.sleep(0))
    return desktop, keys


def test_dictate_with_enter_speaks_full_text_and_pins_window(monkeypatch):
    import asyncio as aio
    from tools import confirm
    window = {"now": "Telegram — Петя"}
    desktop, keys = _dictate_env(monkeypatch, window)
    text = "Буду через полчаса, возьми хлеб и молоко, пожалуйста, и ещё посмотри, не закрыт ли магазин у дома"
    r = aio.run(desktop.call("dictate", {"text": text, "enter": True}, None))
    assert text in r["speak_verbatim"] and "Telegram — Петя" in r["speak_verbatim"]
    window["now"] = "Konsole"  # к «да» фокус ушёл в другое окно
    res = aio.run(confirm.take()["run"]())
    assert res["ok"] is False and not any("ydotool" in a for a in keys)


def test_dictate_refuses_terminal(monkeypatch):
    import asyncio as aio
    desktop, keys = _dictate_env(monkeypatch, {"now": "Konsole — bash"})
    assert aio.run(desktop.call("dictate", {"text": "rm -rf ~"}, None))["ok"] is False and keys == []


def test_screen_click_checks_text_really_on_screen(monkeypatch):
    """Модель сказала «Оп», на экране — «Оплатить»: отказ; «Уда» -> «Удалить» — с вопросом."""
    import asyncio as aio
    from tools import confirm, desktop, screen
    clicks = []
    monkeypatch.setattr(screen, "zoom_active", lambda: False)
    monkeypatch.setattr(screen, "_capture", lambda target: object())
    monkeypatch.setattr(desktop, "_screen_scale", lambda: 1.0)

    async def click_at(x, y):
        clicks.append((x, y))
        return {"ok": True}

    monkeypatch.setattr(desktop, "_click_at", click_at)
    monkeypatch.setattr(desktop, "_find_text", lambda im, t, nth=1: (100, 200, "оплатить"))
    assert aio.run(desktop._screen_click("Оп", 1))["ok"] is False and clicks == []
    monkeypatch.setattr(desktop, "_find_text", lambda im, t, nth=1: (100, 200, "удалить"))
    r = aio.run(desktop._screen_click("Уда", 1))
    assert r["speak_verbatim"] == "Нажать «удалить»?" and clicks == []
    assert aio.run(confirm.take()["run"]())["ok"] and clicks == [(100, 200)]
