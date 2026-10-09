"""Живой режим (LE Audio): нарезка реплик из непрерывного потока и поведение ядра."""
import asyncio
import json

import numpy as np
import pytest

import voice_in

RATE = voice_in.RATE


def frames(*parts):
    x = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype(np.int16)
    return [x[i:i + 320] for i in range(0, len(x) - 319, 320)]


def speech(s, amp=0.1):
    t = np.arange(int(RATE * s)) / RATE
    return amp * np.sin(2 * np.pi * 220 * t)


def zeros(s):
    return np.zeros(int(RATE * s))


def run(seg, fr, decide=(0.9, "")):
    evs, utts = [], []
    for k, f in enumerate(fr):
        ev = seg.push(f)
        if ev == "partial":  # частичное распознавание — отдельные тесты
            continue
        if ev == "check_fast":
            ev = seg.decide(*decide, fast=True)
        if ev == "check":
            ev = seg.decide(*decide)
        if ev:
            evs.append((ev, round(k * 0.02, 2)))
        if ev == "end":
            utts.append(seg.utterance())
    return evs, utts


def test_two_utterances_in_one_stream():
    seg = voice_in.LiveSegmenter({"turn_check_ms": 800, "turn_wait_ms": 2000, "barge_ms": 400})
    evs, utts = run(seg, frames(zeros(1), speech(1.0), zeros(1.5), speech(0.6), zeros(1.5)))
    kinds = [e for e, _ in evs]
    assert kinds == ["start", "long", "end", "start", "long", "end"]
    assert len(utts) == 2
    # 300 мс до начала речи сохраняются: первый слог не теряется
    assert utts[0][1]["audio_s"] == pytest.approx(0.3 + 1.0 + 0.2 - 0.12, abs=0.1)


def test_short_aga_is_not_long():
    seg = voice_in.LiveSegmenter({"barge_ms": 400})
    evs, utts = run(seg, frames(zeros(0.5), speech(0.25), zeros(1.5)))
    kinds = [e for e, _ in evs]
    assert "start" in kinds and "long" not in kinds and kinds[-1] == "end"


def test_hanging_pause_waits_for_continuation():
    seg = voice_in.LiveSegmenter({"turn_check_ms": 800, "turn_wait_ms": 2000, "turn_hang_wait_ms": 3000})
    evs, utts = run(seg, frames(zeros(0.5), speech(0.8), zeros(1.5), speech(0.8), zeros(4)),
                    decide=(0.99, "Расскажи мне."))
    assert len(utts) == 1  # пауза 1,5 с внутри фразы не режет её
    assert utts[0][1]["audio_s"] > 3.0


# --- ядро ---
import core  # noqa: E402


class FakeWS:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def receive_json(self, timeout=None):
        return self.events.pop(0)

    async def receive(self, timeout=None):
        if not self.events:
            await asyncio.sleep(timeout or 0.01)
            raise asyncio.TimeoutError
        ev = self.events.pop(0)
        if isinstance(ev, float):  # пауза в потоке
            await asyncio.sleep(ev)
            raise asyncio.TimeoutError

        class M:
            type = core.aiohttp.WSMsgType.TEXT
            data = json.dumps(ev)
        return M()

    async def close(self):
        self.closed = True


def utt(text):
    return {"type": "utterance", "text": text, "speaker": {}, "timings": {}}


@pytest.fixture
def live_env(monkeypatch):
    said, turns = [], []

    class Sess:
        def __init__(self, ws):
            self.ws = ws

        def ws_connect(self, url, heartbeat=None):
            return self.ws

    async def fake_turn(text, timings, speaker=None, **kw):
        turns.append(text)
        await asyncio.sleep(0.05)

    async def fake_notice(text):
        said.append(text)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(core, "turn", fake_turn)
    monkeypatch.setattr(core, "say_notice", fake_notice)
    monkeypatch.setattr(core, "deliver_waiting", noop)
    monkeypatch.setattr(core.music, "duck", noop)
    monkeypatch.setattr(core.ks, "stop", noop)
    monkeypatch.setitem(core.CONFIG, "live_idle_s", 0.3)
    monkeypatch.setattr(core, "save_agent_note", lambda note, last="": said.append("note:" + note))

    def go(events):
        monkeypatch.setattr(core.ks, "session", Sess(FakeWS(events)))
        lc = core.LiveConversation()
        asyncio.run(lc.run())
        return turns, said

    return go


def test_live_turns_then_idle_close(live_env):
    turns, said = live_env([{"type": "ready"}, utt("Привет."), 0.2, utt("Какая погода?")])
    assert turns == ["Привет.", "Какая погода?"]


def test_live_stop_when_silent_ends(live_env):
    turns, said = live_env([{"type": "ready"}, utt("Стоп."), utt("Привет.")])
    assert turns == []


def test_live_note_is_saved_not_answered(live_env):
    turns, said = live_env([{"type": "ready"}, utt("Заметка: громко"), 0.1])
    assert turns == [] and "note:громко" in said and "Записала." in said


def test_live_falls_back_when_not_duplex(live_env, monkeypatch):
    called = []

    async def fake_run(self):
        called.append(True)
    monkeypatch.setattr(core.Conversation, "run", fake_run)
    live_env([{"type": "error", "reason": "not_duplex"}])
    assert called == [True]


def test_live_hold_words_do_not_get_answered(live_env):
    turns, said = live_env([{"type": "ready"}, utt("Подожди."), utt("Погоди секунду"), utt("Слушай!"), 0.1,
                            utt("А как звали лисичку?")])
    assert turns == ["А как звали лисичку?"]


def test_hold_words():
    assert core.is_hold("Подожди.") and core.is_hold("Ксения, погоди!") and core.is_hold("Секундочку")
    assert not core.is_hold("Подожди, а как звали лисичку?")


def test_feedback_detection():
    for t in ("Да, серьёзно.", "Ага, ага, понял тебя.", "Интересно.", "Ничего себе!", "Круто.",
              "Продолжай, продолжай.", "Нет, ты рассказывай, рассказывай, я тебя буду слушать.", "Давай, рассказывай."):
        assert core.is_backchannel(t), t
    for t in ("А ты в этом уверен?", "Слушай, а расскажи лучше про Млечный путь.", "Подожди, а ты слышала про Войджер?",
              "Включи музыку."):
        assert not core.is_backchannel(t), t
    assert core.is_hold("Сtop.") and core.is_hold("top.")


def test_live_feedback_during_speech_keeps_talking(live_env, monkeypatch):
    stops = []

    async def fake_stop():
        stops.append(1)
    monkeypatch.setattr(core.ks, "stop", fake_stop)
    monkeypatch.setattr(core.LiveConversation, "speaking", staticmethod(lambda: True))

    async def slow_turn(text, timings, speaker=None, **kw):
        await asyncio.sleep(0.5)
    monkeypatch.setattr(core, "turn", slow_turn)
    turns, said = live_env([{"type": "ready"}, utt("Расскажи про Рим."), 0.05, utt("Интересно."), utt("Круто.")])
    assert stops == []  # поддакивания не обрывают рассказ


def test_guest_cannot_stop_ksenia(live_env, monkeypatch):
    stops = []

    async def fake_stop():
        stops.append(1)
    monkeypatch.setattr(core.ks, "stop", fake_stop)
    monkeypatch.setattr(core.LiveConversation, "speaking", staticmethod(lambda: True))

    async def slow_turn(text, timings, speaker=None, **kw):
        await asyncio.sleep(0.5)
    monkeypatch.setattr(core, "turn", slow_turn)
    guest = {"type": "utterance", "text": "Подожди.", "speaker": {"owner": False, "enrolled": True}, "timings": {}}
    turns, said = live_env([{"type": "ready"}, utt("Расскажи про Рим."), 0.05,
                            {"type": "partial", "text": "Подожди", "owner": False}, guest])
    assert stops == []
