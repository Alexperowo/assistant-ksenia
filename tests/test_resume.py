"""Перебили посреди рассказа — потом «ладно, продолжай»: Ксения продолжает с прерванной фразы, а не с начала."""
import asyncio
import json
import time

import pytest

import core
from test_core_respond import call, script_steps  # noqa: F401

STORY = ("Рим основали в восьмом веке до нашей эры. Сначала это был маленький город на холмах. "
         "Потом он подчинил соседей и стал республикой. Через несколько веков республика стала империей.")


def speaker_at(phrases, played_s):
    """Speaker, которому «отдали» фразы целиком (по 1 с звука на 60 символов), а проиграно played_s секунд."""
    sp = core.Speaker(None)
    t = 1000.0
    for text in phrases:
        ph = {"text": text, "start": sp.audio_s, "end": None}
        sp.phrases.append(ph)
        sp._account(int(len(text) / 60 * sp.BYTES_PER_S), now=t)
        ph["end"] = sp.audio_s
    return sp, t + played_s


def test_progress_backs_up_to_sentence_start():
    first = "Рим основали в восьмом веке до нашей эры."
    rest_chunk = STORY[len(first) + 1:]
    sp, now = speaker_at([first, rest_chunk], played_s=len(first) / 60 + 0.5)  # середина «Сначала это был…»
    said, rest = sp.progress(now)
    assert said.startswith(first) and len(said) > len(first)
    assert rest.startswith("Сначала это был маленький город")  # с начала прерванного предложения
    assert rest.endswith("стала империей.")


def test_progress_before_and_after():
    sp, now = speaker_at(["Раз два три.", "Четыре пять."], played_s=0)
    assert sp.progress(now) == ("", "Раз два три. Четыре пять.")
    sp, now = speaker_at(["Раз два три.", "Четыре пять."], played_s=100)
    assert sp.progress(now) == ("Раз два три. Четыре пять.", "")


def test_buffer_gap_is_not_counted_as_played():
    sp = core.Speaker(None)
    sp.phrases.append({"text": "А", "start": 0.0, "end": None})
    sp._account(sp.BYTES_PER_S * 2, now=0.0)  # 2 с звука отдано в момент 0
    sp.phrases[0]["end"] = sp.audio_s
    assert sp.played_s(now=1.0) == pytest.approx(1.0)  # из 2 с отдано — проиграна 1
    sp._account(sp.BYTES_PER_S, now=10.0)  # пауза между фразами: буфер был пуст
    assert sp.played_s(now=10.5) == pytest.approx(2.5)


class ResumeSpeaker:
    instances = []

    def __init__(self, session, output="local"):
        self.output, self.cancelled, self.spoken = output, False, []
        ResumeSpeaker.instances.append(self)

    async def warm(self):
        pass

    async def speak(self, text, timings, verbatim=False):
        self.spoken.append(text)

    async def finish(self):
        pass

    async def cancel(self):
        self.cancelled = True


@pytest.fixture
def ks(monkeypatch, tmp_path):
    ResumeSpeaker.instances = []
    monkeypatch.setattr(core, "Speaker", ResumeSpeaker)
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "history.json"))
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start, k.last_tag, k.speaker, k.session = [], 0, None, None, None
    k.lock, k.last_turn_t, k.interrupted = asyncio.Lock(), time.time(), None
    monkeypatch.setattr(core, "ks", k)
    return k


def test_continue_after_question_resumes_without_brain(ks):
    ks.interrupted = {"said": "Рим основали в восьмом веке до нашей эры. Сначала это был",
                      "rest": "Сначала это был маленький город на холмах. Потом он подчинил соседей.",
                      "complete": True, "t": time.time(), "noted": False}
    brain = []

    async def no_brain(*a, **k):
        brain.append(1)
        return "", [], False
    ks._step = no_brain
    before = list(ks.history)
    asyncio.run(core.turn("Ладно, продолжай.", {"_t0": 0}))
    assert brain == []  # мозг не спрашивали: текст уже был
    assert ResumeSpeaker.instances[0].spoken[0].startswith("Сначала это был маленький город")
    assert ks.history[:len(before)] == before  # только дописывается
    assert ks.history[-1] == {"role": "assistant", "content": "Сначала это был маленький город на холмах. Потом он подчинил соседей."}
    assert ks.interrupted is None


def test_note_tells_the_brain_what_was_really_said(ks):
    ks.interrupted = {"said": "Рим основали в восьмом веке", "rest": "Потом он подчинил соседей.", "complete": True,
                      "t": time.time(), "noted": False}
    script_steps(ks, [("Цезарю было пятьдесят пять.", [])])
    asyncio.run(core.turn("А сколько лет было Цезарю?", {"_t0": 0}))
    user = ks.history[0]["content"]
    assert "тебя перебили" in user and "Рим основали в восьмом веке" in user and "Потом он подчинил" in user
    assert ks.interrupted["noted"] and ks.interrupted["rest"]  # рассказ не забыт: можно вернуться
    script_steps(ks, [("Ок.", [])])
    asyncio.run(core.turn("Понятно, а погода завтра какая?", {"_t0": 0}))
    assert "тебя перебили" not in ks.history[-2]["content"]  # напоминаем один раз


def test_cut_generation_continues_through_brain(ks):
    ks.interrupted = {"said": "Рим", "rest": "основали", "complete": False, "t": time.time(), "noted": True}
    script_steps(ks, [("…в восьмом веке до нашей эры.", [])])
    asyncio.run(core.turn("Продолжай.", {"_t0": 0}))
    assert ks.history[-1]["content"] == "…в восьмом веке до нашей эры."  # недогенерированный — досказывает мозг


def test_stale_interruption_is_ignored(ks):
    ks.interrupted = {"said": "a", "rest": "b", "complete": True, "t": time.time() - 3600, "noted": False}
    script_steps(ks, [("Рассказываю дальше про другое.", [])])
    asyncio.run(core.turn("Продолжай.", {"_t0": 0}))
    assert "тебя перебили" not in ks.history[0]["content"] and ks.history[-1]["content"].startswith("Рассказываю")


def test_interrupting_during_reply_records_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "h.json"))
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start, k.last_tag, k.speaker, k.session = [], 0, None, None, None
    k.lock, k.last_turn_t, k.interrupted = asyncio.Lock(), time.time(), None
    real = core.Speaker

    class Sp(real):
        async def warm(self):
            pass

        async def speak(self, text, timings, verbatim=False):
            # «звучит» мгновенно и полностью: phrases и часы как у настоящего
            ph = {"text": text, "start": self.audio_s, "end": None}
            self.phrases.append(ph)
            self._account(self.BYTES_PER_S * 3)
            ph["end"] = self.audio_s
            await asyncio.sleep(0.05)

        async def finish(self):
            pass

        async def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(core, "Speaker", Sp)

    async def step(budget, queue, speaker, timings, first_step):
        await queue.put(("Рим основали в восьмом веке до нашей эры.", False))
        await queue.put(("Сначала это был маленький город на холмах. Потом он подчинил соседей.", False))
        return STORY, [], False
    k._step = step

    async def go():
        t = asyncio.create_task(k.respond("Расскажи про Рим", {"_t0": 0}))
        await asyncio.sleep(0.03)  # звучит первая фраза
        await k.stop()
        await t

    asyncio.run(go())
    it = k.interrupted
    assert it and it["complete"]
    assert "Сначала это был маленький город" in it["rest"]  # вторая фраза не прозвучала — она в недосказанном
