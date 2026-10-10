"""Детектор речи voice-in на синтетическом сигнале: parec и часы подменены, железа не нужно.

Сигнал идёт кадрами по 20 мс; часы сдвигаются на 20 мс на каждый прочитанный кадр,
поэтому проверки не зависят от скорости машины."""
import asyncio

import numpy as np
import pytest

import voice_in

RATE = voice_in.RATE
FRAME = voice_in.FRAME


def noise(seconds, rms=0.002, seed=0):
    rnd = np.random.default_rng(seed)
    return rnd.normal(0, rms, int(RATE * seconds))


def speech(seconds, amp=0.1, freq=220):
    t = np.arange(int(RATE * seconds)) / RATE
    return amp * np.sin(2 * np.pi * freq * t)


def jbl_speech(seconds):
    """Речь с провалами: микрофон JBL глушит паузы до нуля (40 мс тишины каждые 100 мс)."""
    x = speech(seconds)
    for start in range(0, len(x), int(RATE * 0.1)):
        x[start + int(RATE * 0.06):start + int(RATE * 0.1)] = 0
    return x


def pcm(*parts):
    x = np.concatenate(parts)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t


class FakeParec:
    def __init__(self, data, clock, stall=False):
        self.data, self.pos, self.clock, self.stall = data, 0, clock, stall
        self.stdout = self
        self.returncode = None
        self.killed = False

    async def readexactly(self, n):
        if self.pos + n > len(self.data):
            if self.stall:
                await asyncio.sleep(3600)  # SCO не отдаёт звук: сработает таймаут wait_for
            raise asyncio.IncompleteReadError(self.data[self.pos:], n)
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        self.clock.t += n / 2 / RATE
        return chunk

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def rec(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(voice_in, "time", clock)
    monkeypatch.setattr(voice_in.Ear, "_save_debug", staticmethod(lambda frames: None))
    monkeypatch.setattr(voice_in, "CONFIG", {**voice_in.CONFIG, "start_timeout_s": 3, "max_s": 6})
    procs = []

    def run(data, stall=False, config=None):
        if config:
            voice_in.CONFIG.update(config)
        p = FakeParec(data, clock, stall)
        procs.append(p)

        async def fake_exec(*args, **kw):
            assert args[0] == "parec" and args[2] == "bluez_input.TEST"
            return p

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        if stall:
            real_wait_for = asyncio.wait_for

            async def fast_wait_for(aw, timeout):
                return await real_wait_for(aw, timeout=0.05)

            monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)
        ear = voice_in.Ear.__new__(voice_in.Ear)
        out = asyncio.run(ear.record_utterance("bluez_input.TEST", None))
        assert p.killed  # parec не остаётся жить
        return out

    return run


def test_phrase_between_pauses(rec):
    audio, info = rec(pcm(noise(1.0), speech(1.5), noise(1.5)))
    # начало: 6 громких кадров (min_voiced_ms 120) после 1,0 с
    assert info["speech_start_s"] == pytest.approx(1.12, abs=0.03)
    # начало записи — за 0,6 с до обнаружения речи (тишина после сигнала не нужна ни распознаванию, ни отпечатку),
    # конец: 700 мс тишины, хвост обрезан до 200 мс -> (1,12 − 0,6) … 2,7
    assert info["audio_s"] == pytest.approx(2.18, abs=0.03)
    assert len(audio) == int(info["audio_s"] * RATE)
    assert np.abs(audio[:int(RATE * 0.4)]).max() < 3000  # до речи — только запас тишины
    assert np.abs(audio[int(RATE * 0.6):int(RATE * 1.9)]).max() > 3000  # речь внутри


def test_jbl_gaps_still_count_as_speech(rec):
    audio, info = rec(pcm(noise(0.8), jbl_speech(1.2), noise(1.2)))
    assert info["speech_start_s"] == pytest.approx(0.8, abs=0.25)
    # провалы по 40 мс короче 700 мс — фраза не обрывается посередине
    assert info["audio_s"] > 1.3  # 1,2 с речи + запас в начале и 200 мс хвоста


def test_beep_echo_in_first_350ms_is_ignored(rec):
    audio, info = rec(pcm(speech(0.3, amp=0.3), noise(4.0)))
    assert audio is None and info["reason"] == "no_speech"


def test_quick_word_right_after_loud_beep_tail(rec):
    # живой тест 2026-10-08: хвост сигнала длиннее 350 мс, тишина JBL (ровный ноль), «привет» 0,3 с
    audio, info = rec(pcm(speech(0.4, amp=0.2), np.zeros(int(RATE * 0.55)), speech(0.3, amp=0.2),
                          np.zeros(int(RATE * 2.0))))
    assert audio is not None
    assert info["speech_start_s"] == pytest.approx(1.07, abs=0.05)


class FakeTurn:
    """Smart Turn: «не договорил» на первой паузе, «договорил» на следующих."""
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    def complete(self, pcm):
        self.calls += 1
        return self.answers.pop(0) if self.answers else 0.9


def run_with_turn(rec, monkeypatch, data, answers):
    ft = FakeTurn(answers)
    monkeypatch.setattr(voice_in.Ear, "turn", ft, raising=False)
    return rec(data), ft


def test_turn_mid_phrase_pause_does_not_cut(rec, monkeypatch):
    # пауза 1 с посреди фразы: модель говорит «не договорил» — ждём, вторая часть попадает в запись
    (audio, info), ft = run_with_turn(rec, monkeypatch,
                                      pcm(noise(0.5), speech(1.0), np.zeros(int(RATE * 1.0)), speech(1.0), noise(2.5)),
                                      [0.1, 0.9])
    assert info["turn_p"] == [0.1, 0.9]
    assert info["audio_s"] == pytest.approx(0.5 + 1.0 + 1.0 + 1.0 + 0.2, abs=0.05)


def test_turn_complete_ends_fast(rec, monkeypatch):
    (audio, info), ft = run_with_turn(rec, monkeypatch, pcm(noise(0.5), speech(1.0), noise(2.5)), [0.95])
    assert ft.calls == 1
    assert info["audio_s"] == pytest.approx(0.5 + 1.0 + 0.2, abs=0.05)  # конец через 300 мс, а не 700


def test_turn_incomplete_waits_then_ends(rec, monkeypatch):
    (audio, info), ft = run_with_turn(rec, monkeypatch, pcm(noise(0.5), speech(1.0), noise(3.0)), [0.1])
    assert ft.calls == 1 and info["audio_s"] == pytest.approx(0.5 + 1.0 + 0.2, abs=0.05)
    # ушло 2 с тишины ожидания, хвост обрезан до 200 мс


def test_name_slips_fixed():
    assert voice_in.fix_name("Сеня, посмотри на экран") == "Ксения, посмотри на экран"
    assert voice_in.fix_name("привет, ксенья") == "привет, Ксения"
    assert voice_in.fix_name("Арсеня пришёл") == "Арсеня пришёл"


def test_short_click_is_not_speech(rec):
    audio, info = rec(pcm(noise(1.0), speech(0.06, amp=0.5), noise(3.0)))
    assert audio is None and info["reason"] == "no_speech"


def test_silence_times_out(rec):
    audio, info = rec(pcm(noise(4.0)))
    assert audio is None and info["reason"] == "no_speech"
    assert info["noise_rms"] == pytest.approx(0.002, abs=0.001)


def test_adaptive_threshold_in_noisy_room(rec):
    # шум 0,02 выше min_speech_rms: порог = шум*3, шум сам по себе речью не считается
    audio, info = rec(pcm(noise(3.5, rms=0.02)))
    assert audio is None


def test_long_speech_is_cut_at_max_s(rec):
    audio, info = rec(pcm(noise(0.5), speech(10.0)))
    assert info["audio_s"] == pytest.approx(6.0, abs=0.05)


def test_mic_lost_before_speech(rec):
    audio, info = rec(pcm(noise(0.5)))
    assert audio is None and info == {"reason": "mic_lost"}


def test_mic_lost_mid_phrase_keeps_what_was_said(rec):
    audio, info = rec(pcm(noise(0.6), speech(1.0)))
    assert audio is not None and info["audio_s"] == pytest.approx(1.48, abs=0.03)  # без тишины до речи


def test_mic_stalls(rec):
    audio, info = rec(pcm(noise(0.5)), stall=True)
    assert audio is None and info["reason"] == "mic_lost"


def test_hanging_phrase_waits_even_if_intonation_says_done(rec, monkeypatch):
    # «Расскажи мне…» — Smart Turn уверен (0.99), но фраза оборвана на «мне»: ждём, и продолжение попадает в запись
    texts = iter(["Расскажи мне.", "Расскажи мне какой сегодня день недели."])
    monkeypatch.setattr(voice_in.Ear, "model", object(), raising=False)
    monkeypatch.setattr(voice_in.Ear, "transcribe", lambda self, pcm: next(texts), raising=False)
    (audio, info), ft = run_with_turn(rec, monkeypatch,
                                      pcm(noise(0.5), speech(0.8), np.zeros(int(RATE * 1.2)), speech(1.0), noise(3.0)),
                                      [0.99, 0.99])
    assert info["turn_text"][0].endswith("мне.")
    assert info["audio_s"] == pytest.approx(0.5 + 0.8 + 1.2 + 1.0 + 0.2, abs=0.05)


def test_hanging_words():
    assert voice_in.hanging("Расскажи мне.")
    assert voice_in.hanging("Просто слишком короткие фразы,")
    assert voice_in.hanging("отправить сообщение, э-э")
    assert not voice_in.hanging("Включи русский рок.")
    assert not voice_in.hanging("")


def test_mood_hint_after_baseline():
    m = voice_in.Mood()
    normal = (speech(2.0, amp=0.2) * 32767).astype(np.int16)
    for _ in range(12):
        assert m.hint(normal, "обычная фраза из пяти слов", "le") is None
    tired = (speech(4.0, amp=0.05) * 32767).astype(np.int16)  # тише и медленнее (те же слова за вдвое дольше)
    assert "тише и медленнее" in m.hint(tired, "обычная фраза из пяти слов", "le")
    assert m.hint(normal[:8000], "коротко", "le") is None  # коротко — не судим


def test_question_is_not_hanging():
    import voice_in
    assert not voice_in.hanging("Что?") and not voice_in.hanging("Давай!") and voice_in.hanging("расскажи мне")
    assert voice_in.turn_policy(0.1, "Давай.", {"asked": True}, {})[0] == "end"


def test_live_utterance_ends_despite_steady_background():
    """Фраза, затем ровный фон (телевизор): реплика всё равно кончается по пределу длины."""
    import voice_in
    seg = voice_in.LiveSegmenter({**voice_in.CONFIG, "live_max_s": 5, "smart_turn": False})
    loud = (np.sin(np.arange(320) / 3) * 8000).astype(np.int16)
    hum = (np.sin(np.arange(320) / 5) * 700).astype(np.int16)  # ~0,02 RMS — выше порога конца речи
    evs = [seg.push(loud) for _ in range(50)] + [seg.push(hum) for _ in range(400)]
    assert "end" in evs


def test_hung_inference_exits_for_restart(monkeypatch):
    import asyncio as aio
    import time as t
    import voice_in
    exits = []
    monkeypatch.setitem(voice_in.CONFIG, "infer_timeout_s", 0.1)
    monkeypatch.setattr(voice_in.os, "_exit", lambda code: exits.append(code))
    aio.run(voice_in.infer(t.sleep, 0.5))
    assert exits == [1]
