"""Размышления <think> не звучат вслух, даже если сервер их не отделил и теги пришли по кускам."""
import asyncio
import random

import pytest

import core
from fakes import FakeResponse, FakeSession, sse


def feed_all(chunks):
    f = core.ThinkFilter()
    out = "".join(f.feed(c) for c in chunks)
    return out + f.flush(), f


@pytest.mark.parametrize("text,expected", [
    ("Привет!", "Привет!"),
    ("<think>Хм, он хочет джаз. Включу.</think>Включаю джаз.", "Включаю джаз."),
    ("До <think>мысль</think>после", "До после"),
    ("Сравни 3 < 5 и <b>тег</b>", "Сравни 3 < 5 и <b>тег</b>"),
    ("<think>не закрыто до конца", ""),
    ("<thin", "<thin"),
])
def test_whole_text(text, expected):
    assert feed_all([text])[0] == expected


@pytest.mark.parametrize("seed", range(40))
def test_any_chunking_gives_same_result(seed):
    text = "Начало. <think>Думаю: надо вызвать музыку. Да.</think>Включаю радио. <think>ещё</think>Готово < 3."
    rnd = random.Random(seed)
    cuts = sorted(rnd.sample(range(1, len(text)), rnd.randint(1, 15)))
    chunks = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
    assert feed_all(chunks)[0] == "Начало. Включаю радио. Готово < 3."


def test_lone_closing_tag_drops_what_came_before():
    out, f = feed_all(["Он просит джаз, значит", " включу.</think>Вклю", "чаю."])
    assert out.endswith("Включаю.")


def test_step_does_not_speak_thoughts():
    ks = core.Ksenia.__new__(core.Ksenia)
    ks.history, ks.window_start, ks.last_tag = [{"role": "user", "content": "включи джаз"}], 0, None
    lines = [sse({"content": "<think>Хм, он хочет джаз."}), sse({"content": " Надо вызвать музыку. Сделаю"}),
             sse({"content": " это сейчас.</th"}), sse({"content": "ink>Включаю джаз, "}), sse({"content": "минутку."})]
    ks.session = FakeSession(FakeResponse(200, lines))
    q = asyncio.Queue()

    class Sp:
        cancelled = False

    content, calls, failed = asyncio.run(ks._step(0, q, Sp(), {"_t0": 0}, first_step=True))
    spoken = []
    while not q.empty():
        spoken.append(q.get_nowait()[0])
    assert content == "Включаю джаз, минутку."
    assert " ".join(spoken) == "Включаю джаз, минутку."  # раньше звучало «Надо вызвать музыку. Сделаю это сейчас.»
