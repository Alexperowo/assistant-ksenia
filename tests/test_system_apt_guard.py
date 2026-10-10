"""apt: перед вопросом — пробный прогон. «pipewire-» в install = удалить звук и рабочий стол; установка,
которая что-то удаляет, и удаление важных частей системы — отказ."""
import asyncio

import pytest

from tools import confirm, system


@pytest.fixture
def sim(monkeypatch):
    plan = {}

    async def fake_exec(argv, timeout):
        if argv[:2] == ["apt-get", "-s"]:
            return 0, "\n".join(f"Remv {r} [1.0]" for r in plan.get(tuple(argv[3:]), []))
        raise AssertionError(f"без «да» ничего не исполняется: {argv}")

    monkeypatch.setattr(system, "_exec", fake_exec)
    monkeypatch.setattr(system, "PM", "apt")
    confirm.cancel()
    yield plan
    confirm.cancel()


def run(cmd, p):
    return asyncio.run(system.call("system", {"command": cmd, "param": p}, None))


def test_install_that_removes_audio_is_refused(sim):
    sim[("install", "pipewire-")] = ["pipewire-pulse", "kubuntu-desktop"]
    r = run("install", "pipewire-")
    assert r["ok"] is False and "kubuntu-desktop" in r["would_remove"] and confirm.peek() is None


def test_install_that_removes_anything_is_refused(sim):
    sim[("install", "foo")] = ["bar"]
    assert run("install", "foo")["ok"] is False


def test_plain_install_asks_and_runs_in_background(sim):
    r = run("install", "vlc")
    assert r["speak_verbatim"] == "Установить «vlc»?"
    assert confirm.peek()["background"] is True and confirm.peek()["limit"] == 900


def test_remove_with_dependents_names_them(sim):
    sim[("remove", "vlc")] = ["vlc", "vlc-plugin-base"]
    assert "vlc-plugin-base" in run("remove", "vlc")["speak_verbatim"]


def test_remove_vital_dependency_is_refused(sim):
    sim[("remove", "libfoo")] = ["libfoo", "plasma-pa"]
    assert run("remove", "libfoo")["ok"] is False


def test_install_argv_never_removes():
    assert "--no-remove" in system.CHANGE["install"][2]("vlc") or system.PM != "apt"
