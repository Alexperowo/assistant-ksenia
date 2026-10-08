"""Каталог проверенных команд: ничего вне каталога, параметры не позволяют подсунуть команду, изменения — только через подтверждение."""
import asyncio

from tools import confirm, system


def run(args):
    return asyncio.run(system.call("system", args, None))


def test_unknown_command_refused():
    assert not run({"command": "rm -rf /"})["ok"]


def test_param_injection_refused():
    for bad in ["vlc; rm -rf ~", "vlc && reboot", "-y", "../etc", "VLC MEDIA", "$(id)", "vlc\nreboot"]:
        assert not run({"command": "install", "param": bad})["ok"], bad


def test_protected_package_refused():
    assert not run({"command": "remove", "param": "sudo"})["ok"]
    assert not run({"command": "remove", "param": "plasma-desktop"})["ok"]


def test_change_goes_through_core_confirmation():
    r = run({"command": "install", "param": "cowsay"})
    assert r["prepared"] and r["speak_verbatim"].startswith("Установить")
    assert confirm.current()["label"] == "установить «cowsay»"
    confirm.cancel()


def test_bad_service_name_refused():
    assert not run({"command": "restart_user_service", "param": "x; reboot"})["ok"]
    assert not run({"command": "bluetooth_connect", "param": "00:11"})["ok"]
