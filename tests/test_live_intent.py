"""Классификатор реплики во время речи Ксении: смысл и контекст, судья для спорного, словарь — запасной путь.

Фразы ниже — примеры живой речи, а не список, который код должен знать наизусть: проверяем, что решение
следует из строения реплики и того, что Ксения сейчас делает."""
import asyncio
import json

import pytest

import live_intent as li
from fakes import FakeResponse, FakeSession

SPEAKING = li.Context(state="speaking", said="В пятом веке до нашей эры Рим был небольшим городом.", story=True)
THINKING = li.Context(state="thinking")
IDLE = li.Context(state="idle", said="Рим основали, по легенде, Ромул и Рем.")


@pytest.mark.parametrize("text", [
    "Ничего себе!", "Да, серьёзно.", "Интересно.", "Круто.", "Ага, ага, понял тебя.", "Правда?", "Серьёзно?",
    "А дальше?", "И что потом?", "Продолжай, продолжай.", "Нет, ты рассказывай, рассказывай, я тебя буду слушать.",
    "Да ладно!", "Вот это да", "Ха-ха, смешно", "Угу", "Обалдеть, реально?",
])
def test_reactions_during_story_keep_her_talking(text):
    d = li.quick(text, SPEAKING)
    assert d.sure and d.kind == "continue", (text, d)


@pytest.mark.parametrize("text,kind", [
    ("Подожди.", "hold"), ("Погоди секунду", "hold"), ("Ой, подожди", "hold"), ("Ксения, погоди!", "hold"),
    ("Секундочку", "hold"), ("Слушай!", "hold"), ("Э-э…", "hold"),
    ("Стоп.", "stop"), ("Хватит", "stop"), ("Ксения, стоп", "stop"), ("Хопп.", "stop"), ("Хопер", "stop"),
    ("Stop.", "stop"), ("Сtop", "stop"),
    ("Алло? Да, слушаю.", "aside"),
    ("Подожди, а как звали лисичку?", "question"),
    ("А сколько лет было Цезарю?", "question"),
    ("Слушай, а расскажи лучше про Млечный путь.", "request"),
    ("Включи, пожалуйста, радио с джазом погромче", "request"),
    ("Нет, не так, я про древний Рим, а не про Римскую империю", "correction"),
    ("Ну всё, пока!", "goodbye"),
])
def test_interruptions(text, kind):
    d = li.quick(text, SPEAKING)
    assert d.sure and d.kind == kind, (text, d)
    assert d.interrupts


def test_short_ambiguous_goes_to_judge():
    for text in ("Включи музыку.", "Мой папа тоже", "А Карфаген"):
        d = li.quick(text, SPEAKING)
        assert not d.sure, (text, d)


def test_partial_question_waits_for_the_rest():
    ctx = li.Context(state="speaking", partial=True)
    assert not li.quick("А как", ctx).sure  # он ещё говорит: «а как…» — решим по полной фразе
    assert li.quick("Подожди", ctx).kind == "hold"  # а «подожди» ясно уже по началу


def test_same_words_mean_other_things_when_she_is_silent():
    assert li.quick("Круто.", IDLE).kind == "reaction"  # после ответа — ответить совсем коротко
    assert li.quick("Включи музыку.", IDLE).kind == "request"
    ctx = li.Context(state="idle", interrupted=True)
    assert li.quick("Ладно, продолжай.", ctx).kind == "continue"  # вернуться к недосказанному
    assert li.quick("Ксения", IDLE).kind == "request"  # позвал — ответит «Да?»
    assert li.quick("Ксения", SPEAKING).kind == "hold"  # позвал посреди её речи — замолчать и слушать
    assert li.quick("Где это?", SPEAKING).kind == "question"


def test_noise():
    assert li.quick("", SPEAKING).kind == "noise" and li.quick("м", SPEAKING).kind == "noise"


def test_judge_prompt_has_context():
    msgs = li.judge_prompt("Да ну?", SPEAKING)
    user = msgs[1]["content"]
    assert "длинный рассказ" in user and "Рим был небольшим" in user and "«Да ну?»" in user
    assert li.parse_judge("A") == "continue" and li.parse_judge(" d\n") == "question" and li.parse_judge("?") is None


def judge_answer(letter):
    return FakeResponse(200, body=json.dumps({"choices": [{"message": {"content": letter}}]}))


def test_judge_decides_ambiguous_and_caches():
    j = li.Judge({"brain_url": "http://brain", "live_judge_timeout_s": 1}, key="k")
    s = FakeSession(judge_answer("A"))

    async def go():
        d1 = await li.decide("А Карфаген", SPEAKING, j, s)
        d2 = await li.decide("А Карфаген", SPEAKING, j, s)  # запомнено: второй раз без запроса
        return d1, d2

    d1, d2 = asyncio.run(go())
    assert d1.kind == "continue" and d1.source == "judge" and d2.source == "judge"
    url, kw = s.requests[0]
    assert len(s.requests) == 1 and kw["json"]["id_slot"] == 1 and kw["json"]["max_tokens"] == 2
    assert kw["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert kw["headers"]["Authorization"] == "Bearer k"


def test_dedicated_small_model_has_no_slot():
    j = li.Judge({"brain_url": "http://brain", "live_judge_url": "http://judge"})
    s = FakeSession(judge_answer("E"))
    d = asyncio.run(li.decide("Включи музыку.", SPEAKING, j, s))
    assert d.kind == "request" and s.requests[0][0] == "http://judge/v1/chat/completions"
    assert "id_slot" not in s.requests[0][1]["json"]


def test_judge_timeout_falls_back_to_dictionary():
    class Slow:
        def post(self, *a, **k):
            class R:
                async def __aenter__(self):
                    await asyncio.sleep(5)

                async def __aexit__(self, *a):
                    return False
            return R()

    j = li.Judge({"brain_url": "http://brain", "live_judge_timeout_s": 0.05})
    d = asyncio.run(li.decide("Включи музыку.", SPEAKING, j, Slow()))
    assert d.source == "fallback" and d.kind == "request" and d.ms < 1000


def test_judge_off_uses_fallback():
    j = li.Judge({"brain_url": "http://brain", "live_judge": False})
    d = asyncio.run(li.decide("Мой папа тоже", SPEAKING, j, FakeSession()))
    assert d.source == "fallback"


def test_decision_log(tmp_path, monkeypatch):
    monkeypatch.setattr(li, "DECISIONS_FILE", str(tmp_path / "d.jsonl"))
    li.log_decision("Круто.", SPEAKING, li.Decision("continue", True), acted="дальше")
    rec = json.loads((tmp_path / "d.jsonl").read_text(encoding="utf-8"))
    assert rec["kind"] == "continue" and rec["state"] == "speaking" and rec["acted"] == "дальше"
