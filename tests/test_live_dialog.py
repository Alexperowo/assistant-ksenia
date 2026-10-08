"""Живой диалог целиком (фальшивый поток слуха): поддакивания не обрывают рассказ, «подожди» останавливает
по первым словам, ложная тревога — продолжение с того же места, договорил пока думала — одна реплика."""
import asyncio
import json

import pytest

import core
import live_intent


class Stream:
    """Поток событий слуха; числа — паузы в секундах. Запоминает, что ядро прислало обратно (контекст)."""

    def __init__(self, events):
        self.events, self.sent, self.closed = list(events), [], False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def receive_json(self, timeout=None):
        return self.events.pop(0)

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self, timeout=None):
        if not self.events:
            await asyncio.sleep(timeout or 0.01)
            raise asyncio.TimeoutError
        ev = self.events.pop(0)
        if isinstance(ev, (int, float)):
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


def part(text):
    return {"type": "partial", "text": text}


@pytest.fixture
def live(monkeypatch, tmp_path):
    rec = {"turns": [], "stops": 0, "said": [], "judge": []}
    state = {"speaking": False}

    class Sess:
        def __init__(self, ws):
            self.ws = ws

        def ws_connect(self, url, heartbeat=None):
            return self.ws

        def post(self, url, json=None, **kw):  # судья
            rec["judge"].append(json["messages"][1]["content"])
            letter = rec.get("judge_letter", "A")

            class R:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def json(self, **k):
                    return {"choices": [{"message": {"content": letter}}]}
            return R()

    async def fake_turn(text, timings, speaker=None, resume=False, **kw):
        rec["turns"].append((text, resume))
        state["speaking"] = True
        try:
            await asyncio.sleep(rec.get("turn_s", 0.6))
        finally:
            state["speaking"] = False

    async def fake_stop():
        rec["stops"] += 1
        if state["speaking"]:  # как настоящий respond: перебили — запомнить недосказанное
            core.ks.interrupted = {"said": "Рим основали", "rest": "Потом он вырос.", "complete": True,
                                   "t": __import__("time").time(), "noted": False}
        state["speaking"] = False

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(core, "turn", fake_turn)
    monkeypatch.setattr(core, "say_notice", lambda text: noop())
    monkeypatch.setattr(core, "deliver_waiting", noop)
    monkeypatch.setattr(core.music, "duck", noop)
    monkeypatch.setattr(core.ks, "stop", fake_stop)
    monkeypatch.setattr(core.ks, "interrupted", None)
    monkeypatch.setattr(core.LiveConversation, "speaking", staticmethod(lambda: state["speaking"]))
    monkeypatch.setitem(core.CONFIG, "live_idle_s", 0.4)
    monkeypatch.setattr(live_intent, "DECISIONS_FILE", str(tmp_path / "d.jsonl"))

    def go(events, **opts):
        rec.update(opts)
        ws = Stream([{"type": "ready"}] + events)
        monkeypatch.setattr(core.ks, "session", Sess(ws))
        asyncio.run(core.LiveConversation().run())
        rec["sent"] = ws.sent
        rec["log"] = [json.loads(x) for x in (tmp_path / "d.jsonl").read_text(encoding="utf-8").splitlines()] \
            if (tmp_path / "d.jsonl").exists() else []
        return rec

    return go


def test_reactions_do_not_break_the_story(live):
    r = live([utt("Расскажи историю Рима."), 0.1, part("Ничего"), utt("Ничего себе!"), 0.05, utt("Да, серьёзно?"),
              utt("Интересно."), 0.1, utt("Круто.")], turn_s=0.8)
    assert r["turns"] == [("Расскажи историю Рима.", False)] and r["stops"] == 0
    assert r["judge"] == []  # ясные реакции — без судьи
    assert all(x["acted"] == "говорит дальше" for x in r["log"][1:])


def test_hold_stops_on_first_words_then_answers_question(live):
    r = live([utt("Расскажи про лисичку."), 0.1, part("Подожди"), 0.05,
              utt("Подожди, а как звали лисичку?")], turn_s=0.8)
    assert r["stops"] >= 1 and r["turns"][-1] == ("Подожди, а как звали лисичку?", False)
    assert any(x["acted"] == "замолчала по началу фразы" for x in r["log"])


def test_false_alarm_resumes_from_same_place(live):
    r = live([utt("Расскажи про Рим."), 0.1, part("Ой подожди"), 0.05, utt("Ой, подожди… ничего себе!")], turn_s=0.8)
    assert r["stops"] >= 1  # замолчала на «подожди»
    assert r["turns"][-1][1] is True  # и продолжила с того же места, а не ответила на «ничего себе»


def test_hold_then_continue(live):
    r = live([utt("Расскажи про Рим."), 0.1, utt("Погоди."), 0.1, utt("Ладно, продолжай.")], turn_s=0.8)
    assert r["turns"][0] == ("Расскажи про Рим.", False) and r["turns"][-1] == ("Ладно, продолжай.", True)


def test_continued_phrase_while_thinking_is_merged(live, monkeypatch):
    monkeypatch.setattr(core.LiveConversation, "speaking", staticmethod(lambda: False))  # ещё думает
    r = live([utt("Расскажи мне про Рим"), 0.1, utt("и про Карфаген.")], turn_s=0.8)
    assert r["turns"][-1] == ("Расскажи мне про Рим и про Карфаген.", False)


def test_lost_onset_stop_during_story(live):
    r = live([utt("Расскажи сказку."), 0.1, part("Хопп"), utt("Хопп.")], turn_s=0.8)
    assert r["stops"] >= 1 and len(r["turns"]) == 1


def test_ambiguous_goes_to_judge_with_context(live):
    r = live([utt("Расскажи про Рим."), 0.1, utt("Мой папа тоже")], turn_s=0.8, judge_letter="A")
    assert r["judge"] and "Ксения сейчас говорит" in r["judge"][0] and "«Мой папа тоже»" in r["judge"][0]
    assert r["stops"] == 0 and len(r["turns"]) == 1
    assert r["log"][-1]["source"] == "judge"


def test_question_interrupts(live):
    r = live([utt("Расскажи про Рим."), 0.1, utt("А сколько лет было Цезарю, когда он перешёл Рубикон?")], turn_s=0.8)
    assert r["stops"] >= 1 and r["turns"][-1][0].startswith("А сколько лет")


def test_context_is_sent_to_hearing(live):
    r = live([utt("Расскажи про Рим."), 0.3], turn_s=0.3)
    states = [m["ksenia"] for m in r["sent"] if m.get("type") == "context"]
    assert states[0] == "idle" and "speaking" in states and states[-1] == "idle"


def test_stop_when_silent_ends_live_mode(live):
    r = live([utt("Стоп."), utt("Привет.")])
    assert r["turns"] == []


def test_own_backchannel_when_he_tells_a_long_story(live, monkeypatch):
    said = []

    async def bc(self):
        said.append(1)
        self.last_bc = __import__("time").time()

    monkeypatch.setattr(core.LiveConversation, "backchannel", bc)
    monkeypatch.setitem(core.CONFIG, "live_backchannels", True)
    live([{"type": "pause", "speech_s": 9.5}, 0.05, {"type": "pause", "speech_s": 12}, 0.05,
          {"type": "pause", "speech_s": 2}])
    assert said == [1]  # одно «угу» за длинный рассказ, не на каждой паузе и не на короткой фразе


def test_own_backchannel_is_off_by_default(live, monkeypatch):
    said = []
    monkeypatch.setattr(core.LiveConversation, "backchannel", lambda self: said.append(1))
    live([{"type": "pause", "speech_s": 20}])
    assert said == []
