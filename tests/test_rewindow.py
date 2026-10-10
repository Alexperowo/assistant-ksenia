"""Окно истории: прыжок — заранее, в тишине, с прогревом мозга; в разговоре начало запроса не меняется."""
import asyncio

import core


class Resp:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.data


class Sess:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/apply-template"):
            return Resp({"prompt": "PROMPT"})
        return Resp({"timings": {"prompt_n": 5000}})


def make_ks(n):
    k = core.Ksenia.__new__(core.Ksenia)
    k.history = []
    for i in range(n):
        k.history += [{"role": "user", "content": f"вопрос {i}"}, {"role": "assistant", "content": f"ответ {i}"}]
    k.window_start, k.system, k.session = 0, "S", Sess()
    k.facts_seen = []
    return k


def test_fill_and_jump_keeps_recent_half(monkeypatch):
    monkeypatch.setitem(core.CONFIG, "history_max", 20)
    k = make_ks(15)  # 30 сообщений из 20
    monkeypatch.setattr(k, "build_system", lambda: None)
    assert k.window_fill() > 1
    k._jump()
    assert k.window_fill() <= 0.55 and k.history[k.window_start]["role"] in ("user", "assistant")


def test_prewarm_renders_with_tools_and_counts_prompt_only():
    k = make_ks(3)
    assert asyncio.run(k.prewarm()) is True
    (u1, b1), (u2, b2) = k.session.posts
    assert u1.endswith("/apply-template") and b1["tools"] == core.TOOL_SCHEMAS
    assert b1["chat_template_kwargs"] == {"enable_thinking": False}
    assert b1["messages"][-1]["role"] == "user"  # до последней реплики Александра: дальше шаблон рисует иначе
    assert u2.endswith("/completion") and b2["n_predict"] == 0 and b2["prompt"] == "PROMPT"
    assert b2["id_slot"] == core.CONFIG.get("brain_slot", 0)
