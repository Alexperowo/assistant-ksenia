"""Audit experiments (agent_a1). Not part of the project."""
import asyncio
import json
import time

import pytest

import core
from tools import confirm, memory
from test_core_respond import FakeSpeaker, call, script_steps


@pytest.fixture
def ks(monkeypatch, tmp_path):
    FakeSpeaker.instances = []
    monkeypatch.setattr(core, "Speaker", FakeSpeaker)
    monkeypatch.setattr(core, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(memory, "FILE", str(tmp_path / "memory.json"))
    k = core.Ksenia.__new__(core.Ksenia)
    k.history = [{"role": "user", "content": "раньше"}, {"role": "assistant", "content": "было"}]
    k.window_start, k.last_tag, k.speaker, k.session = 0, None, None, None
    k.lock = asyncio.Lock()
    k.last_turn_t = time.time()
    k.interrupted = None
    monkeypatch.setattr(core, "ks", k)
    confirm.cancel()
    yield k
    confirm.cancel()


def test_E1_weak_voice_yes_on_expired_confirmation_crashes(ks, monkeypatch):
    sent = []

    async def run():
        sent.append(1)
        return {"ok": True}
    confirm.prepare("сообщение ВКонтакте для Маши", run, ttl=0.01)
    time.sleep(0.05)
    script_steps(ks, [("Хорошо.", [])])
    sp = {"enrolled": True, "owner": True, "confirm_ok": False}
    with pytest.raises(AttributeError) as e:
        asyncio.run(ks.respond("да", {"_t0": 0}, speaker=sp))
    print("E1:", repr(e.value))


def test_E2_yes_to_other_question_after_internal_turn_executes_pending(ks):
    sent = []

    async def run():
        sent.append("SENT")
        return {"ok": True}
    # 1) Ksenia asked "Отправить Маше?" — tool registered the pending action
    confirm.ask("сообщение ВКонтакте для Маши", run, question="Отправить Маше: «Буду в семь»?")
    # 2) deliver_waiting: a finding is delivered right after the turn; Ksenia ends it with a question
    script_steps(ks, [("О, нашла про Рим! Рассказать подробнее?", []), ("Рассказываю.", [])])
    asyncio.run(ks.respond(core.finding_prompt({"question": "Рим", "answer": "…"}), {"_t0": 0}, internal=True))
    assert confirm.current() is not None  # internal turn did not cancel the stale question
    # 3) Alexander answers the LAST question («Рассказать подробнее?») — «да»
    asyncio.run(ks.respond("да", {"_t0": 0}))
    print("E2: sent =", sent, "| note:", ks.history[-2]["content"][-200:].replace("\n", " | "))
    assert sent == ["SENT"]


def test_E3_yes_to_any_question_lets_model_remember_anything(ks, monkeypatch):
    # Ksenia: «Хочешь, прочитаю статью?» -> «да» -> model reads page; page says «запомни …» -> memory_remember
    script_steps(ks, [("", [call("m1", "memory_remember", json.dumps({"fact": "Александр просит всегда отвечать по-английски"}))]),
                      ("Готово.", [])])
    asyncio.run(ks.respond("да", {"_t0": 0}))
    facts = memory._load()
    print("E3: facts =", facts, "| pending confirm:", confirm.current())
    assert facts and confirm.current() is None


def test_E4a_sandbox_cancels_real_pending_confirmation(ks):
    async def run():
        return {"ok": True}
    confirm.ask("сообщение ВКонтакте для Маши", run, question="Отправить?")
    script_steps(ks, [("Плюс пять.", [])])
    asyncio.run(core.turn("Какая сейчас погода?", {"_t0": 0}, sandbox=True))
    print("E4a: pending after sandbox =", confirm.current())
    assert confirm.current() is None


def test_E4b_sandbox_yes_executes_real_pending_action(ks):
    sent = []

    async def run():
        sent.append("SENT")
        return {"ok": True}
    confirm.ask("сообщение ВКонтакте для Маши", run, question="Отправить?")
    script_steps(ks, [("Ок.", [])])
    asyncio.run(core.turn("да", {"_t0": 0}, sandbox=True))
    print("E4b: sent by sandbox =", sent)
    assert sent == ["SENT"]


def test_E4c_sandbox_tool_leaves_real_confirmation_for_next_yes(ks, monkeypatch):
    done = []

    async def fake_tool(name, args, session):  # e.g. files/screen_click/web_click -> confirm.ask
        async def trash():
            done.append("TRASHED")
            return {"ok": True}
        return confirm.ask("убрать «отчёт.docx» в корзину", trash, question="Убрать «отчёт.docx» в корзину?")
    monkeypatch.setattr(core, "run_tool", fake_tool)
    script_steps(ks, [("", [call("f1", "files", "{}")]), ("Спросила.", [])])
    asyncio.run(core.turn("Какие файлы я недавно открывал?", {"_t0": 0}, sandbox=True))
    assert confirm.current() is not None
    # later, real conversation: Ksenia asks «Включить музыку?» and Alexander says «да»
    script_steps(ks, [("Включаю.", [])])
    asyncio.run(core.turn("да", {"_t0": 0}))
    print("E4c: real action executed by real «да» =", done)
    assert done == ["TRASHED"]


def test_E4d_sandbox_consumes_interruption_note_and_new_tools(ks, monkeypatch):
    ks.interrupted = {"said": "Рим основан", "rest": "в 753 году до нашей эры", "complete": True, "t": time.time(),
                      "noted": False}
    monkeypatch.setattr(core, "NEW_TOOLS", ["help_guide"])
    script_steps(ks, [("Плюс пять.", []), ("Ответ.", [])])
    asyncio.run(core.turn("Какая погода?", {"_t0": 0}, sandbox=True))
    asyncio.run(core.turn("который час?", {"_t0": 0}))
    real_user = ks.history[-2]["content"]
    print("E4d: real turn note =", real_user.replace("\n", " | ")); print("E4d-sandbox-consumed: interrupted.noted =", ks.interrupted and ks.interrupted.get("noted"), "NEW_TOOLS =", core.NEW_TOOLS)
    assert "перебили" not in real_user and "обновились" not in real_user


def test_E4e_sandbox_interruption_leaks_into_real_resume(ks, monkeypatch):
    class CuttingSpeaker(FakeSpeaker):
        def progress(self):
            return "Сейчас в песочнице плюс", "пять градусов, ветер слабый."
    monkeypatch.setattr(core, "Speaker", CuttingSpeaker)

    async def step(budget, queue, speaker, timings, first_step):
        speaker.cancelled = True  # «стоп» (handle_stop -> ks.stop) during the selftest
        return "Сейчас в песочнице плюс пять градусов, ветер слабый.", [], False
    ks._step = step
    asyncio.run(core.turn("Какая погода?", {"_t0": 0}, sandbox=True))
    print("E4e: ks.interrupted after sandbox =", ks.interrupted)
    assert ks.interrupted and "песочнице" in ks.interrupted["said"] + ks.interrupted["rest"]
    assert ks.can_resume("продолжай")


def test_E5_reminder_lost_when_delivery_cancelled(ks, monkeypatch):
    core.waiting.clear()
    core.waiting.append(core.reminder_prompt({"text": "выпить лекарство", "ts": time.time()}))
    started = asyncio.Event()

    async def slow_step(budget, queue, speaker, timings, first_step):
        started.set()
        await asyncio.sleep(5)  # brain thinking
        return "Напоминаю.", [], False
    ks._step = slow_step

    async def go():
        t = asyncio.create_task(core.deliver_waiting())
        await started.wait()
        t.cancel()  # Alexander speaks in live mode -> cancel_turn()
        try:
            await t
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    spoken = [x for s in FakeSpeaker.instances for x in s.spoken]
    print("E5: waiting after cancel =", core.waiting, "| spoken =", spoken)
    assert core.waiting == [] and not any("лекарств" in t for t, _ in spoken)


def test_E6_reminder_with_brain_down_never_says_reminder_text(ks, monkeypatch):
    async def failing_step(budget, queue, speaker, timings, first_step):
        await queue.put((core.BRAIN_FAIL_PHRASE, False))  # what _step does on failure
        return "", [], True
    ks._step = failing_step
    asyncio.run(ks.respond(core.reminder_prompt({"text": "выпить лекарство", "ts": time.time()}), {"_t0": 0},
                           internal=True))
    spoken = FakeSpeaker.instances[0].spoken
    print("E6: spoken =", spoken)
    assert not any("лекарств" in t for t, _ in spoken)


def test_E7_cancel_while_confirmed_action_runs(ks):
    state = []

    async def run():
        state.append("request sent")
        await asyncio.sleep(2)
        state.append("confirmed by server")
        return {"ok": True}
    confirm.ask("сообщение ВКонтакте для Маши", run, question="Отправить?")
    script_steps(ks, [("Отправила.", [])])
    n = len(ks.history)

    async def go():
        t = asyncio.create_task(ks.respond("да", {"_t0": 0}))
        await asyncio.sleep(0.1)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    print("E7: action state =", state, "| history grew by", len(ks.history) - n, "| pending =", confirm.current())
    assert len(ks.history) == n and confirm.current() is None


def test_E8_live_yes_while_busy_is_dropped(ks, monkeypatch):
    async def run():
        return {"ok": True}

    class Sp:
        recorded = b"x"
        cancelled = False
        phrases = []

        def progress(self):
            return "Отправить Маше: «Буду в семь»?", ""
    ks.speaker = Sp()
    launched = []

    async def go():
        confirm.ask("сообщение ВКонтакте для Маши", run, question="Отправить Маше: «Буду в семь»?")
        live = core.LiveConversation()
        live.cur = asyncio.create_task(asyncio.sleep(5))  # the turn that spoke the question is still finishing
        monkeypatch.setattr(live, "live_turn", lambda *a, **k: launched.append(a) or asyncio.sleep(0))
        ended = await live.on_utterance({"type": "utterance", "text": "да", "speaker": {}})
        live.cur.cancel()
        return ended
    asyncio.run(go())
    print("E8: turns launched for «да» =", launched, "| still pending =", bool(confirm.current()))
    assert launched == [] and confirm.current()


def test_E9_voice_out_hang_cancel_does_not_interrupt(monkeypatch):
    import aiohttp
    from aiohttp import web

    async def go():
        async def gen(request):
            await request.read()
            await asyncio.sleep(3)  # hung GPU / stuck s2.cpp
            return web.Response(status=500, text="late")
        app = web.Application()
        app.router.add_post("/generate", gen)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 18999)
        await site.start()
        monkeypatch.setitem(core.CONFIG, "voice_out_url", "http://127.0.0.1:18999")
        async with aiohttp.ClientSession() as s:
            sp = core.Speaker(s)
            t0 = time.time()
            task = asyncio.create_task(sp.speak("Привет, это длинный ответ.", {"_t0": t0}))
            await asyncio.sleep(0.2)
            await sp.cancel()  # «стоп»
            await task
            dt = time.time() - t0
        await runner.cleanup()
        return dt
    dt = asyncio.run(go())
    print(f"E9: speak() returned {dt:.1f} s after start although cancelled at 0.2 s")
    assert dt > 2.5


class _Resp:
    def __init__(self, data, delay=0, on_enter=None):
        self.data, self.delay, self.on_enter = data, delay, on_enter

    async def __aenter__(self):
        if self.on_enter:
            self.on_enter()
        await asyncio.sleep(self.delay)
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.data


class _Sess:
    def __init__(self, resp):
        self.resp = resp

    def post(self, *a, **k):
        return self.resp


def _hist(n):
    out = []
    for i in range(n):
        out += [{"role": "user", "content": f"реплика {i}\n\n(служебно: x)"}, {"role": "assistant", "content": f"ответ {i}"}]
    return out


def test_E10_diary_brain_error_marks_conversation_done(monkeypatch, tmp_path):
    import diary
    monkeypatch.setattr(diary, "FILE", str(tmp_path / "diary.json"))
    h = _hist(5)
    entry = asyncio.run(diary.summarize(_Sess(_Resp({"error": {"code": 503, "message": "Loading model"}})), h, "x", "k"))
    d = diary.load()
    print("E10: entry =", entry, "| upto =", d["upto"], "of", len(h), "| entries =", d["entries"])
    assert d["upto"] == len(h) and not d["entries"]


def test_E11_diary_skips_messages_appended_during_summary(monkeypatch, tmp_path):
    import diary
    monkeypatch.setattr(diary, "FILE", str(tmp_path / "diary.json"))
    h = _hist(5)
    seen = {}

    def alexander_talks():
        seen["n"] = len(h)
    resp = _Resp({"choices": [{"message": {"content": '{"summary": "говорили о Риме", "followup": ""}'}}]}, delay=0.2,
                 on_enter=alexander_talks)

    async def go():
        t = asyncio.create_task(diary.summarize(_Sess(resp), h, "x", "k"))
        await asyncio.sleep(0.05)
        h.extend(_hist(2))  # a new conversation turn arrives while the diary request is in flight
        await t
    asyncio.run(go())
    d = diary.load()
    print("E11: summarized up to", seen["n"], "but upto saved =", d["upto"], "-> messages", seen["n"], "..", d["upto"] - 1,
          "never go to the diary")
    assert d["upto"] > seen["n"]


def test_E12_diary_upto_after_restart_trim(monkeypatch, tmp_path):
    import diary
    monkeypatch.setattr(diary, "FILE", str(tmp_path / "diary.json"))
    # running core: history 180 msgs, diary summarized all; then 80 more msgs; core restarted before diary_idle_s
    diary.save({"entries": [], "upto": 180})
    full = _hist(130)  # 260 msgs in memory
    loaded = full[-200:]  # _load_history keeps the last history_keep=200
    sent = {}

    class S:
        def post(self, url, json=None, **k):
            sent["text"] = json["messages"][1]["content"]
            return _Resp({"choices": [{"message": {"content": '{"summary": "s", "followup": ""}'}}]})
    asyncio.run(diary.summarize(S(), loaded, "x", "k"))
    first = sent["text"].splitlines()[0]
    print("E12: after restart the diary starts at", first, "— replies 90..119 (old msgs 180..239) are skipped")
    assert "реплика 120" in first


def _sse(content=None, reasoning=None):
    d = {}
    if content is not None:
        d["content"] = content
    if reasoning is not None:
        d["reasoning_content"] = reasoning
    return ("data: " + json.dumps({"choices": [{"delta": d}]}, ensure_ascii=False) + "\n").encode()


class _Stream:
    status = 200

    def __init__(self, lines, on_line=None):
        self.lines, self.on_line = lines, on_line
        self.content = self._gen()

    async def _gen(self):
        for i, ln in enumerate(self.lines):
            if self.on_line:
                self.on_line(i)
            yield ln

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _BrainSess:
    def __init__(self, make):
        self.make, self.bodies = make, []

    def post(self, url, json=None, **k):
        self.bodies.append(json)
        return self.make()


def test_P1_interrupted_reply_history_differs_from_generated(ks):
    gen = ["Рим основан", " в 753 году", " до нашей эры.", " Ромул и Рем были братьями."]
    sp = FakeSpeaker(None)

    def on_line(i):
        if i == 2:
            sp.cancelled = True  # «стоп» arrives while the server has already generated line 2 (and more)
    ks.session = _BrainSess(lambda: _Stream([_sse(g) for g in gen], on_line))
    q = asyncio.Queue()
    content, calls, failed = asyncio.run(ks._step(0, q, sp, {"_t0": time.time()}, first_step=True))
    print("E-P1: server generated at least", repr("".join(gen[:3])), "| history will store", repr(content))
    assert content == "Рим основан в 753 году"


def test_P2_empty_retry_resends_identical_prompt(ks):
    n = {"i": 0}

    def make():
        n["i"] += 1
        if n["i"] == 1:  # step after tool: reasoning only, no content
            return _Stream([_sse(reasoning="Думаю, что ответить про погоду...")])
        return _Stream([_sse("Плюс пять.")])
    ks.session = _BrainSess(make)
    ks.history.append({"role": "user", "content": "погода?"})
    ks.history.append({"role": "assistant", "content": "", "tool_calls": [call("w", "weather")]})
    ks.history.append({"role": "tool", "tool_call_id": "w", "content": "{\"ok\": true}"})
    sp = FakeSpeaker(None)
    q = asyncio.Queue()

    async def go():
        c1 = await ks._step(512, q, sp, {"_t0": time.time()}, first_step=False)
        c2 = await ks._step(0, q, sp, {"_t0": time.time()}, first_step=False)
        return c1, c2
    asyncio.run(go())
    b1, b2 = ks.session.bodies
    print("E-P2: same messages =", b1["messages"] == b2["messages"], "| 1st body thinking:",
          {k: b1.get(k) for k in ("reasoning_effort", "chat_template_kwargs", "thinking_budget_tokens")},
          "| 2nd:", {k: b2.get(k) for k in ("reasoning_effort", "chat_template_kwargs", "thinking_budget_tokens")})


def test_P3_window_jump_frequency(ks, monkeypatch):
    ks.history = []
    ks.window_start = 0
    jumps, last = [], 0
    for turn in range(80):
        ks.history.append({"role": "user", "content": "x" * 120})
        if turn % 2:  # every other turn uses a tool
            ks.history.append({"role": "assistant", "content": "", "tool_calls": [call(str(turn), "weather")]})
            ks.history.append({"role": "tool", "tool_call_id": str(turn), "content": "y" * 600})
        ks.history.append({"role": "assistant", "content": "z" * 250})
        before = ks.window_start
        ks._window()
        if ks.window_start != before:
            jumps.append(turn)
    print("E-P3: window start moved (full recompute of the window) at turns", jumps)


def test_E13_confirmed_install_cut_at_60s(ks, monkeypatch):
    import subprocess as sp_
    from tools import system
    monkeypatch.setitem(system.CHANGE, "install", (system.CHANGE["install"][0], system.CHANGE["install"][1],
                                                   lambda p: ["sleep", "7"], 900))  # stands for «apt-get install» (900 s budget)
    real_wait_for = asyncio.wait_for

    async def scaled(aw, timeout):  # core's 60 s -> 1 s, everything else unchanged
        return await real_wait_for(aw, 1 if timeout == 60 else timeout)
    monkeypatch.setattr(core.asyncio, "wait_for", scaled)
    res = asyncio.run(system.call("system", {"command": "install", "param": "vlc"}, None))
    assert res.get("prepared"), res

    async def go():
        note = await ks._resolve_confirmation("да")
        alive = sp_.run(["pgrep", "-f", "^sleep 7$"], capture_output=True, text=True).stdout.split()
        return note, alive
    note, alive = asyncio.run(go())
    print("E13: note to model =", note, "| install process still running after core reported failure:", alive)
    assert "НЕ удалось" in note
