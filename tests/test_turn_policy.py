"""Конец реплики для русского и микрофона, глушащего паузы: текст важнее «уверенности» Smart Turn."""
import numpy as np
import pytest

import turn
import voice_in

CFG = {"turn_threshold": 0.5, "turn_wait_ms": 2000, "turn_hang_wait_ms": 3000, "turn_check_ms": 800,
       "turn_fast_ms": 400, "partial_ms": 300, "partial_min_ms": 300, "partial_max_s": 6, "barge_ms": 400}


@pytest.mark.parametrize("text", ["Расскажи мне", "Включи, пожалуйста, э-э", "А потом и", "Я хотел сказать, что",
                                  "Мне нужно…", "Найди в интернете про"])
def test_hanging_phrase_waits_even_if_smart_turn_is_sure(text):
    assert voice_in.turn_policy(0.99, text, {}, CFG) == ("wait", 3000)


@pytest.mark.parametrize("text,ctx", [("Как дела?", {}), ("Включи музыку!", {}), ("Да.", {"asked": True}),
                                      ("Нет, не надо.", {"asked": True}), ("Давай второй", {"asked": True})])
def test_clear_end_is_taken_on_fast_check(text, ctx):
    assert voice_in.turn_policy(0.1, text, ctx, CFG, fast=True) == ("end", None)


def test_fast_check_does_not_end_ordinary_statement():
    assert voice_in.turn_policy(0.99, "Включи музыку.", {}, CFG, fast=True) == ("wait", None)
    assert voice_in.turn_policy(0.99, "Да.", {}, CFG, fast=True) == ("wait", None)  # без вопроса Ксении «да» — не ответ


def test_regular_check_trusts_intonation_when_text_is_neutral():
    assert voice_in.turn_policy(0.9, "Включи музыку.", {}, CFG) == ("end", None)
    assert voice_in.turn_policy(0.2, "Включи музыку.", {}, CFG) == ("wait", 2000)


def frames(*parts):
    x = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype(np.int16)
    return [x[i:i + 320] for i in range(0, len(x) - 319, 320)]


def speech(s):
    return 0.1 * np.sin(2 * np.pi * 220 * np.arange(int(16000 * s)) / 16000)


def zeros(s):
    return np.zeros(int(16000 * s))


def test_question_ends_after_fast_pause():
    seg = voice_in.LiveSegmenter(CFG)
    out, t = [], 0
    for k, f in enumerate(frames(zeros(0.3), speech(1.0), zeros(1.0))):
        ev = seg.push(f)
        if ev == "check_fast":
            ev = seg.decide(1.0, "Который час?", fast=True)
        elif ev == "check":
            ev = seg.decide(0.99, "Который час?")
        if ev == "end":
            t = k * 0.02
            out.append(seg.utterance())
    assert len(out) == 1 and t == pytest.approx(0.3 + 1.0 + 0.4, abs=0.06)  # 0,4 с паузы, а не 0,8


def test_partials_while_speaking_only_at_the_start():
    seg = voice_in.LiveSegmenter(CFG)
    evs = [seg.push(f) for f in frames(zeros(0.3), speech(9.0))]
    partial_t = [k * 0.02 for k, e in enumerate(evs) if e == "partial"]
    assert partial_t and partial_t[0] < 1.0
    assert all(b - a == pytest.approx(0.3, abs=0.03) for a, b in zip(partial_t, partial_t[1:]))
    assert partial_t[-1] < 6.6  # длинную фразу целиком каждые 0,3 с не распознаём


def test_context_from_core_changes_the_decision():
    seg = voice_in.LiveSegmenter(CFG)
    seg.ctx.update({"asked": True})
    assert seg.decide(0.1, "Нет.", fast=True) == "end"
    seg.ctx.update({"asked": False})
    assert seg.decide(0.1, "Нет.", fast=True) is None


def test_dither_fills_only_long_digital_silence():
    x = np.concatenate([np.full(1600, 3000, np.int16), np.zeros(3200, np.int16), np.full(1600, -3000, np.int16),
                        np.zeros(50, np.int16)])
    y = turn.dither_zeros(x)
    assert np.count_nonzero(y[1600:4800]) > 3000  # 200 мс нулей -> тихий шум
    assert np.all(y[4800 + 1600:] == 0)            # 3 мс нулей — не трогаем
    assert np.array_equal(y[:1600], x[:1600])       # речь не меняется
    assert np.abs(y[1600:4800]).max() < 300        # шум тихий (речь — 3000)
    assert np.array_equal(turn.dither_zeros(np.full(800, 5, np.int16)), np.full(800, 5, np.int16))
