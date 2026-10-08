"""Ядро и мозг: окно истории, разбор потока (текст и tool_calls)."""
import asyncio

import pytest

import core
from fakes import FakeResponse, FakeSession, sse


@pytest.fixture
def ks(monkeypatch):
    monkeypatch.setitem(core.CONFIG, "history_max", 10)
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start, k.last_tag = [], 0, None
    k.lock = asyncio.Lock()
    k.speaker = None
    k.session = None
    return k


def msgs(*roles):
    return [{"role": r, "content": f"{r}{i}"} for i, r in enumerate(roles)]


def test_window_does_not_slide_every_turn(ks):
    ks.history = msgs(*(["user", "assistant"] * 5))  # 10 сообщений = предел
    w1 = ks._window()
    ks.history += msgs("user")
    ks.history[-1]["content"] = "new"
    # 11 > 10: окно прыгает вперёд на половину, а не сдвигается на одно сообщение
    w2 = ks._window()
    assert w1[0] == ks.history[0]
    assert len(w2) <= 6 and w2[-1]["content"] == "new"
    start = ks.window_start
    ks.history += msgs("assistant", "user")
    assert ks.window_start == start and ks._window()[0] is w2[0]  # следующие реплики только дописываются


def test_window_starts_on_user_message(ks):
    ks.history = msgs("user", "assistant", "tool", "assistant", "tool", "assistant", "user", "assistant",
                      "tool", "assistant", "user")
    w = ks._window()
    assert w[0]["role"] == "user"


def run_step(ks, *responses, budget=0, cancelled=False):
    ks.session = FakeSession(*responses)
    q = asyncio.Queue()

    class Sp:
        pass

    sp = Sp()
    sp.cancelled = cancelled
    timings = {"_t0": 0}

    async def go():
        res = await ks._step(budget, q, sp, timings, first_step=True)
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return res, items

    ks.history = [{"role": "user", "content": "привет"}]
    return asyncio.run(go()), ks.session.requests


def test_step_streams_text_first_sentence_then_rest(ks):
    lines = [sse({"role": "assistant"}), sse({"content": "Привет, Александр"}), sse({"content": "! Как ты "}),
             sse({"content": "сегодня?"}), "data: [DONE]\n"]
    ((content, calls, _failed), items), _ = run_step(ks, FakeResponse(200, lines))
    assert content == "Привет, Александр! Как ты сегодня?"
    assert calls == []
    texts = [i[0] if isinstance(i, tuple) else i for i in items]
    assert texts == ["Привет, Александр!", "Как ты сегодня?"]


def test_step_collects_streamed_tool_calls(ks):
    lines = [
        sse({"content": "Включаю джаз."}),
        sse({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                             "function": {"name": "music_", "arguments": ""}}]}),
        sse({"tool_calls": [{"index": 0, "function": {"name": "play", "arguments": "{\"query\": "}}]}),
        sse({"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "music_status", "arguments": "{}"}}]}),
        sse({"tool_calls": [{"index": 0, "function": {"arguments": "\"jazz\"}"}}]}),
        "data: [DONE]\n",
    ]
    ((content, calls, _), _items), reqs = run_step(ks, FakeResponse(200, lines), budget=256)
    assert content == "Включаю джаз."
    assert [c["id"] for c in calls] == ["c1", "c2"]
    assert calls[0]["function"] == {"name": "music_play", "arguments": "{\"query\": \"jazz\"}"}
    assert calls[1]["function"]["name"] == "music_status"
    body = reqs[0][1]["json"]
    assert body["thinking_budget_tokens"] == 256 and body["stream"] is True
    assert body["messages"][0]["role"] == "system"


def test_step_ignores_reasoning_and_keepalive_lines(ks):
    lines = [": keep-alive\n", "\n", sse({"reasoning_content": "думаю..."}), sse({"content": "Ага, поняла тебя."})]
    ((content, _, _), _), _ = run_step(ks, FakeResponse(200, lines))
    assert content == "Ага, поняла тебя."


def queued_text(items):
    return " ".join(i[0] for i in items)


def test_step_http_error_says_so_aloud(ks):
    ((content, calls, failed), items), _ = run_step(ks, FakeResponse(503, body='{"error":"Loading model"}'))
    assert failed and calls == [] and content == ""
    assert "Мозг не отвечает" in queued_text(items)


def test_step_timeout_is_handled(ks):
    # общий таймаут aiohttp — TimeoutError, а не ClientError: раньше он ронял ход и оставлял озвучку висеть
    ((content, calls, failed), items), _ = run_step(ks, TimeoutError())
    assert failed and "Мозг не отвечает" in queued_text(items)


def test_step_connection_error_is_handled(ks):
    import aiohttp
    ((_, _, failed), items), _ = run_step(ks, aiohttp.ClientConnectionError("refused"))
    assert failed and "Мозг не отвечает" in queued_text(items)


def test_step_error_event_in_stream(ks):
    lines = [sse({"content": "Сейчас"}), sse(raw='{"error": {"code": 500, "message": "context overflow"}}')]
    ((content, calls, failed), items), _ = run_step(ks, FakeResponse(200, lines))
    assert failed and content == "Сейчас"
    assert queued_text(items).startswith("Сейчас") and "Мозг не отвечает" in queued_text(items)


def test_step_broken_stream_drops_half_tool_call(ks):
    import aiohttp
    lines = [sse({"content": "Включаю."}),
             sse({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "music_play", "arguments": "{\"qu"}}]})]
    resp = FakeResponse(200, lines, error=aiohttp.ClientPayloadError("oops"))
    ((content, calls, failed), _), _ = run_step(ks, resp)
    assert failed and calls == []  # половину команды не исполняем


def test_step_skips_malformed_and_usage_only_lines(ks):
    lines = [sse(raw="{oops"), sse(raw='{"choices": [], "usage": {"total_tokens": 5}}'), sse(raw="[1, 2]"),
             sse({"content": "Всё хорошо, я тут."}), "data: [DONE]\n"]
    ((content, _, failed), _), _ = run_step(ks, FakeResponse(200, lines))
    assert not failed and content == "Всё хорошо, я тут."


def test_step_cancelled_returns_no_calls_and_no_speech(ks):
    lines = [sse({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "music_status", "arguments": "{}"}}]})]
    ((content, calls, failed), items), _ = run_step(ks, FakeResponse(200, lines), cancelled=True)
    assert calls == [] and items == [] and not failed


def test_step_does_not_repeat_last_emotion_tag(ks):
    ks.last_tag = "laughing"
    lines = [sse({"content": "[laughing] Ну ты и шутник! "}), sse({"content": "Ладно."})]
    ((content, _, _), items), _ = run_step(ks, FakeResponse(200, lines))
    assert content.startswith("[laughing]")  # в историю — как сгенерировано
    assert items[0][0] == "Ну ты и шутник!"


def test_parse_stream_line():
    assert core.parse_stream_line(b"data: [DONE]") == (None, None)
    assert core.parse_stream_line(b": ping") == (None, None)
    assert core.parse_stream_line('data: {"choices":[{"delta":{"content":"ё"}}]}'.encode()) == ({"content": "ё"}, None)
    d, err = core.parse_stream_line(b'data: {"error": "boom"}')
    assert d is None and "boom" in err


def test_merge_tool_call_without_index_defaults_to_zero():
    calls = {}
    core.merge_tool_call(calls, {"function": {"name": "read_more"}})
    core.merge_tool_call(calls, {"function": {"arguments": "{}"}})
    assert calls[0]["function"] == {"name": "read_more", "arguments": "{}"}
