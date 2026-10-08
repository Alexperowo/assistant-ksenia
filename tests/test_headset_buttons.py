"""Касания наушников (MPRIS): раскладка, действия ядра и помощник на настоящей шине сеанса — без наушников."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

import core

IDLE = {"speaking": False, "conversation": False, "music": None}


def st(**kw):
    return {**IDLE, **kw}


# --- раскладка ---

@pytest.mark.parametrize("event", ["PlayPause", "Play", "Pause", "Next", "Previous"])
def test_any_tap_while_she_speaks_hushes(event):
    assert core.button_action(event, st(speaking=True, music="playing", conversation=True)) == "hush"


def test_play_and_pause_mean_the_same_tap():
    # наушники чередуют Play и Pause по-своему — смысл не должен зависеть от того, что они прислали
    for state in (IDLE, st(music="playing"), st(music="paused"), st(conversation=True)):
        acts = {core.button_action(e, state) for e in ("PlayPause", "Play", "Pause")}
        assert len(acts) == 1, state


def test_single_tap():
    assert core.button_action("PlayPause", IDLE) == "talk"
    assert core.button_action("PlayPause", st(conversation=True)) == "end"
    assert core.button_action("PlayPause", st(music="playing")) == "music_pause"
    assert core.button_action("PlayPause", st(music="paused")) == "music_resume"
    assert core.button_action("PlayPause", st(music="playing", conversation=True)) == "music_pause"
    assert core.button_action("PlayPause", st(music="paused", conversation=True)) == "end"


def test_double_and_triple_tap():
    assert core.button_action("Next", st(music="playing")) == "music_next"
    assert core.button_action("Next", IDLE) == "talk"
    assert core.button_action("Next", st(music="paused")) == "talk"  # «слушай меня» — даже при музыке на паузе
    assert core.button_action("Previous", st(music="playing")) == "music_previous"
    assert core.button_action("Previous", IDLE) == "repeat"


def test_stop_and_unknown():
    assert core.button_action("Stop", st(speaking=True)) == "stop_all"
    assert core.button_action("Stop", IDLE) == "stop_all"
    assert core.button_action("Seek", IDLE) == "none"
    assert core.button_action("Seek", st(speaking=True)) == "none"


# --- действия ядра ---

@pytest.fixture
def env(monkeypatch):
    log = []

    async def fake_music_call(name, args, session):
        log.append(("music", args["action"]))
        return {"ok": True}

    async def fake_stop():
        log.append(("ks.stop",))

    async def fake_stop_conversation():
        log.append(("end",))
        return True

    async def fake_start_talk():
        log.append(("talk",))

    async def fake_notice(text):
        log.append(("say", text))

    monkeypatch.setattr(core.music, "call", fake_music_call)
    monkeypatch.setattr(core.ks, "stop", fake_stop)
    monkeypatch.setattr(core, "stop_conversation", fake_stop_conversation)
    monkeypatch.setattr(core, "start_talk", fake_start_talk)
    monkeypatch.setattr(core, "say_notice", fake_notice)
    monkeypatch.setitem(core.CONFIG, "headset_buttons_debounce_s", 0.35)
    return log


def press(event, state, monkeypatch):
    monkeypatch.setattr(core, "button_state", lambda: state)
    b = core.HeadsetButtons()
    asyncio.run(b.on_event(event))
    return b


def test_tap_pauses_music(env, monkeypatch):
    press("Play", st(music="playing"), monkeypatch)
    assert env == [("music", "pause")]


def test_tap_while_speaking_stops_speech_only(env, monkeypatch):
    press("PlayPause", st(speaking=True, conversation=True), monkeypatch)
    assert env == [("ks.stop",)]


def test_tap_in_silent_conversation_ends_it_audibly(env, monkeypatch):
    press("PlayPause", st(conversation=True), monkeypatch)
    assert env == [("end",), ("say", "Отдыхаю.")]  # без звука непонятно, сработало ли


def test_stop_all_pauses_music_silently(env, monkeypatch):
    monkeypatch.setattr(core.music, "playing", lambda: True)
    press("Stop", st(music="playing"), monkeypatch)
    assert env == [("end",), ("music", "pause")]


def test_double_tap_starts_talk(env, monkeypatch):
    press("Next", IDLE, monkeypatch)
    assert env == [("talk",)]


def test_one_press_delivered_twice_acts_once(env, monkeypatch):
    monkeypatch.setattr(core, "button_state", lambda: st(music="playing"))
    b = core.HeadsetButtons()

    async def go():
        await b.on_event("PlayPause")
        await b.on_event("Pause")  # та же кнопка через второй путь (медиаклавиша и mpris-proxy)
    asyncio.run(go())
    assert env == [("music", "pause")]


class FakeSpeaker:
    spoken = []

    def __init__(self, session, output="local"):
        self.cancelled = False

    async def warm(self):
        pass

    async def speak(self, text, timings, verbatim=False):
        FakeSpeaker.spoken.append(text)

    async def finish(self):
        pass


def test_triple_tap_repeats_last_reply_without_touching_history(env, monkeypatch):
    FakeSpeaker.spoken = []
    monkeypatch.setattr(core, "Speaker", FakeSpeaker)
    hist = [{"role": "user", "content": "Расскажи про Луну."},
            {"role": "assistant", "content": "[warm] Луна всегда повёрнута к нам одной стороной."},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]}]
    monkeypatch.setattr(core.ks, "history", list(hist))
    monkeypatch.setattr(core.ks, "speaker", None)
    press("Previous", IDLE, monkeypatch)
    assert FakeSpeaker.spoken == ["[warm] Луна всегда повёрнута к нам одной стороной."]
    assert core.ks.history == hist  # повтор — мимо истории


def test_repeat_with_empty_history(env, monkeypatch):
    monkeypatch.setattr(core.ks, "history", [])
    press("Previous", IDLE, monkeypatch)
    assert env == [("say", "Я пока ничего не говорила.")]


def test_player_status_follows_ksenia_and_music(monkeypatch):
    b = core.HeadsetButtons()
    monkeypatch.setattr(core, "button_state", lambda: st(speaking=True))
    assert b.status() == ("Playing", "Ксения говорит")
    monkeypatch.setattr(core, "button_state", lambda: st(music="paused"))
    monkeypatch.setattr(core.music, "title", lambda: "Джаз")
    assert b.status() == ("Paused", "Джаз")
    monkeypatch.setattr(core, "button_state", lambda: IDLE)
    assert b.status() == ("Paused", "Ксения")  # «пауза», а не «стоп»: KDE продолжает отдавать кнопки


# --- связь с помощником (поддельный помощник: тот же протокол строк JSON) ---

FAKE_HELPER = textwrap.dedent("""
    import json, sys
    print(json.dumps({"ready": True}), flush=True)
    print(json.dumps({"event": "PlayPause"}), flush=True)
    line = sys.stdin.readline()
    with open(sys.argv[0] + ".state", "a") as f:
        f.write(line)
""")


def test_run_reads_events_and_sends_state(tmp_path, monkeypatch):
    helper = tmp_path / "helper.py"
    helper.write_text(FAKE_HELPER)
    events = []

    async def fake_on_event(self, ev):
        events.append(ev)
    monkeypatch.setattr(core.HeadsetButtons, "HELPER", str(helper))
    monkeypatch.setattr(core.HeadsetButtons, "on_event", fake_on_event)
    monkeypatch.setattr(core, "button_state", lambda: IDLE)
    monkeypatch.setitem(core.CONFIG, "headset_buttons_python", sys.executable)
    monkeypatch.setitem(core.CONFIG, "headset_buttons_retry_s", 0)
    asyncio.run(asyncio.wait_for(core.HeadsetButtons().run(), 20))
    assert events == ["PlayPause"] * 5  # помощник «падал» — ядро поднимало его снова (5 попыток)
    states = [json.loads(x) for x in (tmp_path / "helper.py.state").read_text().splitlines()]
    assert states and states[0] == {"status": "Paused", "title": "Ксения"}


def test_run_gives_up_when_helper_cannot_start(tmp_path, monkeypatch):
    helper = tmp_path / "broken.py"
    helper.write_text("raise SystemExit(3)\n")  # нет gi или шины сеанса
    monkeypatch.setattr(core.HeadsetButtons, "HELPER", str(helper))
    monkeypatch.setitem(core.CONFIG, "headset_buttons_python", sys.executable)
    t0 = time.time()
    asyncio.run(asyncio.wait_for(core.HeadsetButtons().run(), 20))
    assert time.time() - t0 < 5  # без повторов: бесполезно


# --- настоящий помощник на частной шине сеанса (если в системе есть gi и dbus) ---

def system_python_with_gi():
    for py in ("/usr/bin/python3", "/usr/bin/python3.12", "/usr/bin/python3.13"):
        if os.path.exists(py) and subprocess.run(
                [py, "-c", "import gi; gi.require_version('Gio', '2.0'); from gi.repository import Gio"],
                capture_output=True).returncode == 0:
            return py
    return None


SMOKE = textwrap.dedent("""
    import json, subprocess, sys, time
    py, helper = sys.argv[1], sys.argv[2]
    p = subprocess.Popen([py, helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert json.loads(p.stdout.readline()).get("ready")
    p.stdin.write(json.dumps({"status": "Playing", "title": "Ксения говорит"}) + "\\n"); p.stdin.flush()
    time.sleep(0.3)
    def call(*args):
        return subprocess.run(["gdbus", "call", "--session", "-d", "org.mpris.MediaPlayer2.ksenia",
                               "-o", "/org/mpris/MediaPlayer2", "-m", *args],
                              capture_output=True, text=True, check=True).stdout
    for m in ("PlayPause", "Next", "Previous"):
        call("org.mpris.MediaPlayer2.Player." + m)
    status = call("org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player", "PlaybackStatus")
    events = [json.loads(p.stdout.readline())["event"] for _ in range(3)]
    p.stdin.close()
    p.wait(timeout=5)
    print(json.dumps({"events": events, "status": status.strip(), "code": p.returncode}))
""")


def test_real_helper_on_private_session_bus(tmp_path):
    py = system_python_with_gi()
    if not py or not shutil.which("dbus-run-session") or not shutil.which("gdbus"):
        pytest.skip("нет системного python с gi или dbus")
    script = tmp_path / "smoke.py"
    script.write_text(SMOKE)
    helper = os.path.join(os.path.dirname(core.__file__), "tools", "mpris_helper.py")
    r = subprocess.run(["dbus-run-session", "--", py, str(script), py, helper],
                       capture_output=True, text=True, timeout=30)
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["events"] == ["PlayPause", "Next", "Previous"]
    assert "Playing" in res["status"]
    assert res["code"] == 0  # stdin закрыт (ядро ушло) — помощник выходит сам
