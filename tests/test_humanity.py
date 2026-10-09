"""Человечность в голосе: женский род о себе, пометки эмоций не в каждом ответе, без «Хочешь ещё?» после каждого
ответа. Всё это — только в голосе: история остаётся как сгенерирована (кэш мозга)."""
import asyncio

import pytest

import core
from fakes import FakeResponse, FakeSession, sse


@pytest.mark.parametrize("src,dst", [
    ("Я понял тебя.", "Я поняла тебя."), ("я рад, что ты спросил", "я рада, что ты спросил"),
    ("Я уже сказал.", "Я уже сказала."), ("Я не уверен.", "Я не уверена."), ("Я пошёл бы", "Я пошла бы"),
    ("Я вышел", "Я вышла"), ("Я тебе говорил", "Я тебе говорила"), ("Я сам не знаю", "Я сама не знаю"),
    ("Я согласен.", "Я согласна."), ("Я не мог", "Я не могла"), ("Я был там", "Я была там"),
    ("Я прочёл", "Я прочла"),
])
def test_feminine(src, dst):
    assert core.feminine(src) == dst


@pytest.mark.parametrize("text", ["Он сказал, что я права.", "Я поняла.", "Яблоко упал на стол",
                                  "Ты сказал, а я слушаю.", "Мой брат сказал"])
def test_feminine_leaves_others(text):
    assert core.feminine(text) == text


def test_verbatim_text_is_not_changed():
    # чужое сообщение ВК читается дословно: «Я понял» в нём — слова другого человека
    # знак ударения (U+0301) слов не меняет — его ставит словарь ударений для голоса
    plain = lambda t: t.replace("\u0301", "")
    assert plain(core.clean_for_speech("Дима: Я понял, приду.", verbatim=True)) == "Дима: Я понял, приду."
    assert plain(core.clean_for_speech("Я понял.")) == "Я поняла."


@pytest.mark.parametrize("text,offer", [
    ("Рим основали в 753 году до нашей эры. Хочешь, расскажу про Ромула?", "Хочешь, расскажу про Ромула?"),
    ("Вот такая история. Рассказать ещё что-нибудь?", "Рассказать ещё что-нибудь?"),
    ("Готово, включила джаз. Как тебе?", "Как тебе?"),
])
def test_tail_offer_found(text, offer):
    kept, last = core.split_tail_offer(text)
    assert last == offer and kept and not kept.endswith("?")


@pytest.mark.parametrize("text", ["Хочешь ещё?", "Рим основали в 753 году. Почему именно там — до сих пор спорят.",
                                  "Где ты это слышал? Интересно."])
def test_tail_offer_not_found(text):
    assert core.split_tail_offer(text) == (text, "")


def make_ks(history=None):
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start, k.last_tag, k.speaker, k.session = list(history or []), 0, None, None, None
    return k


def step_spoken(k, lines):
    k.session = FakeSession(FakeResponse(200, lines))
    q = asyncio.Queue()

    class Sp:
        cancelled = False

    content, calls, failed = asyncio.run(k._step(0, q, Sp(), {"_t0": 0}, first_step=True))
    spoken = []
    while not q.empty():
        spoken.append(q.get_nowait()[0])
    return content, " ".join(spoken)


def test_offer_dropped_when_last_reply_also_offered():
    k = make_ks([{"role": "user", "content": "расскажи про Рим"}])
    k.recent_offers = [True]
    content, spoken = step_spoken(k, [sse({"content": "Рим стоит на семи холмах. "}),
                                      sse({"content": "Его основали братья. Хочешь ещё?"})])
    assert content.endswith("Хочешь ещё?")  # история — как сгенерировано
    assert not spoken.endswith("?") and "семи холмах" in spoken


def test_offer_dropped_after_his_short_reaction():
    k = make_ks([{"role": "user", "content": "Круто!\n\n(служебно: …)"}])
    content, spoken = step_spoken(k, [sse({"content": "Да, сама удивилась! "}), sse({"content": "Рассказать ещё?"})])
    assert spoken == "Да, сама удивилась!"


def test_offer_kept_when_it_is_fresh():
    k = make_ks([{"role": "user", "content": "Включи что-нибудь спокойное"}])
    content, spoken = step_spoken(k, [sse({"content": "Включила тихий джаз. "}), sse({"content": "Как тебе?"})])
    assert spoken.endswith("Как тебе?")


def test_emotion_tag_not_in_every_reply():
    k = make_ks([{"role": "user", "content": "привет"}])
    k.recent_tags = [None, "teasing"]  # недавно уже была пометка
    content, spoken = step_spoken(k, [sse({"content": "[excited] Ого, привет! Как ты?"})])
    assert content.startswith("[excited]") and not spoken.startswith("[")
    k2 = make_ks([{"role": "user", "content": "привет"}])
    k2.recent_tags = [None, None]
    _, spoken2 = step_spoken(k2, [sse({"content": "[excited] Ого, привет! Как ты?"})])
    assert spoken2.startswith("[excited]")
