"""Дневник разговоров: что записывается, как попадает в подсказку."""
import asyncio
import datetime as dt
import json

import pytest

import diary
from fakes import FakeResponse, FakeSession


@pytest.fixture(autouse=True)
def tmp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(diary, "FILE", str(tmp_path / "diary.json"))
    diary.changed["flag"] = False


H = [{"role": "user", "content": "Завтра еду в город к врачу.\n\n(служебно: время)"},
     {"role": "assistant", "content": "[warm] Удачи! Во сколько?"},
     {"role": "tool", "content": "{}"},
     {"role": "user", "content": "(служебно: напоминание)"},
     {"role": "user", "content": "В десять утра."},
     {"role": "assistant", "content": "Поставлю напоминание."},
     {"role": "user", "content": "Спасибо."}]


def test_dialogue_text_skips_service_and_tools():
    t = diary.dialogue_text(H, 0)
    assert "служебно" not in t and "{}" not in t and "[warm]" not in t
    assert t.splitlines()[0] == "Александр: Завтра еду в город к врачу."


def test_summarize_writes_entry_and_prompt_block():
    answer = json.dumps({"summary": "Александр завтра едет к врачу в 10 утра.", "followup": "как прошёл приём?"},
                        ensure_ascii=False)
    s = FakeSession(FakeResponse(200, body=json.dumps({"choices": [{"message": {"content": answer}}]})))
    e = asyncio.run(diary.summarize(s, H, "http://brain", "k"))
    assert e["followup"] == "как прошёл приём?" and diary.changed["flag"]
    assert diary.load()["upto"] == len(H)
    block = diary.prompt_block(today=dt.date.today())
    assert ": Александр завтра едет к врачу" in block and "можно спросить" in block
    # тот же кусок второй раз не записывается: реплик после upto нет
    assert asyncio.run(diary.summarize(s, H, "http://brain", "k")) is None


def test_short_or_cleared_history_is_skipped():
    s = FakeSession()
    assert asyncio.run(diary.summarize(s, H[:2], "http://brain", "k")) is None  # мало реплик
    diary.save({"entries": [], "upto": 100})
    assert asyncio.run(diary.summarize(s, H, "http://brain", "k")) is None  # история очищена
    assert diary.load()["upto"] == len(H)


def test_when_words():
    today = dt.date(2026, 10, 9)
    # числом всегда: подсказка живёт в кэше мозга, «сегодня» после полуночи стало бы неправдой
    assert diary._when("2026-10-09", today) == "9 октября" and diary._when("2026-10-08", today) == "8 октября"
    assert diary._when("2026-10-01", today) == "1 октября"


def test_position_survives_history_trim_and_brain_error(monkeypatch, tmp_path):
    """После перезапуска история короче (на диске — последние 200), но место дневника находится по отпечатку;
    ошибка мозга не помечает разговор записанным."""
    import asyncio
    monkeypatch.setattr(diary, "FILE", str(tmp_path / "diary.json"))
    hist = [{"role": "user", "content": f"реплика {i}"} if i % 2 == 0 else {"role": "assistant", "content": f"ответ {i}"}
            for i in range(20)]
    diary.save({"entries": [], "upto": 18, "last_fp": diary._fp(hist[17])})
    trimmed = hist[6:]  # «перезапуск»: первые 6 обрезаны

    class R:
        def __init__(self, status, data):
            self.status, self.data = status, data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self, content_type=None):
            return self.data

    class S:
        def __init__(self, resp):
            self.resp = resp

        def post(self, *a, **k):
            return self.resp

    trimmed += [{"role": "user", "content": f"новая {i}"} for i in range(4)]
    asyncio.run(diary.summarize(S(R(503, {"error": "Loading model"})), trimmed, "u", "k", min_turns=3))
    assert diary.load()["upto"] == 18  # ошибка — место не сдвинуто
    ok = {"choices": [{"message": {"content": '{"summary": "говорили о новом", "followup": ""}'}}]}
    asyncio.run(diary.summarize(S(R(200, ok)), trimmed, "u", "k", min_turns=3))
    d = diary.load()
    assert d["entries"][-1]["summary"] == "говорили о новом" and d["last_fp"] == diary._fp(trimmed[-1])
