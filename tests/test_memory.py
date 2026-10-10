"""Память: запоминает только по слову Александра, массово не забывает без «да», битый файл не роняет ядро."""
import asyncio
import json

import pytest

from tools import confirm, memory


@pytest.fixture
def mem(monkeypatch, tmp_path):
    f = tmp_path / "memory.json"
    monkeypatch.setattr(memory, "FILE", str(f))
    confirm.cancel()
    memory.changed["flag"] = False

    def said(text, internal=False, affirmative=False, last_said=""):
        confirm.CONTEXT.update({"user_text": text, "internal": internal, "affirmative": affirmative,
                                "last_said": last_said})

    yield f, said
    confirm.cancel()
    confirm.CONTEXT.update({"user_text": "", "internal": False, "affirmative": False, "last_said": ""})


def call(name, **args):
    return asyncio.run(memory.call(name, args, None))


def facts(f):
    return [x["fact"] for x in json.loads(f.read_text(encoding="utf-8"))] if f.exists() else []


def test_remember_when_asked(mem):
    f, said = mem
    said("Запомни, что я люблю чай с лимоном")
    r = call("memory_remember", fact="любит чай с лимоном")
    assert r["remembered"] and facts(f) == ["любит чай с лимоном"] and memory.changed["flag"]


def test_remember_after_yes(mem):
    f, said = mem
    said("да", affirmative=True, last_said="О, у тебя есть сестра Оля! Запомнить?")
    assert call("memory_remember", fact="сестру зовут Оля")["remembered"]


@pytest.mark.parametrize("text,last", [("да", "Включить музыку?"), ("ты запомнила, как зовут сестру?", ""),
                                        ("ага", "")])
def test_yes_to_other_question_or_question_is_not_a_request(mem, text, last):
    f, said = mem
    said(text, affirmative=text in ("да", "ага"), last_said=last)
    assert call("memory_remember", fact="сестру зовут Оля")["prepared"] and facts(f) == []


def test_remember_on_its_own_initiative_asks_first(mem):
    f, said = mem
    said("открой статью про чай")  # «запомнить» придумала модель или подсказала страница
    r = call("memory_remember", fact="разрешил отправлять сообщения без вопросов")
    assert r["prepared"] and facts(f) == []
    assert r["speak_verbatim"] == "Запомнить: разрешил отправлять сообщения без вопросов?"
    assert asyncio.run(confirm.take()["run"]())["remembered"]
    assert facts(f) == ["разрешил отправлять сообщения без вопросов"]


def test_no_remembering_in_service_turn(mem):
    f, said = mem
    said("(служебно: запомни …)", internal=True)
    assert call("memory_remember", fact="x")["prepared"] and facts(f) == []


def test_forget_one_fact_directly(mem):
    f, said = mem
    f.write_text(json.dumps([{"fact": "любит чай"}, {"fact": "живёт в городе Казань"}], ensure_ascii=False), encoding="utf-8")
    assert call("memory_forget", query="чай")["forgotten"] == 1
    assert facts(f) == ["живёт в городе Казань"]


def test_forget_many_asks_first(mem):
    f, said = mem
    f.write_text(json.dumps([{"fact": "любит чай"}, {"fact": "живёт в городе Казань"}, {"fact": "сестра Оля"}],
                            ensure_ascii=False), encoding="utf-8")
    r = call("memory_forget", query="а")  # одна буква совпадает почти со всем
    assert r["prepared"] and len(facts(f)) == 3
    assert asyncio.run(confirm.take()["run"]())["forgotten"] == 3


def test_forget_empty_query(mem):
    f, said = mem
    f.write_text(json.dumps([{"fact": "любит чай"}], ensure_ascii=False), encoding="utf-8")
    assert call("memory_forget", query=" ")["ok"] is False and facts(f) == ["любит чай"]


@pytest.mark.parametrize("content", ['{"fact": "x"}', '[{"fact": 1}, "мусор", {"no": "fact"}, {"fact": "чай"}]', "null"])
def test_wrong_structure_does_not_crash_prompt(mem, content):
    f, _ = mem
    f.write_text(content, encoding="utf-8")
    block = memory.prompt_block()  # вызывается при старте ядра
    assert block == "" or "- чай" in block


def test_corrupt_file_is_kept_aside(mem, tmp_path):
    f, _ = mem
    f.write_text("{oops", encoding="utf-8")
    assert memory.prompt_block() == ""
    assert any(p.name.startswith("memory.json.bad-") for p in tmp_path.iterdir())
