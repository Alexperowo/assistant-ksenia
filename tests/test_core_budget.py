"""Динамический бюджет: болтовня — без размышлений, просьба сделать — немного подумать."""
import pytest

import core


def budget(text):
    return core.Ksenia.budget_for(None, text)


ACTION = core.CONFIG.get("budget_action", 256)
CHAT = core.CONFIG.get("budget_chat", 0)


@pytest.mark.parametrize("text", [
    "Включи радио", "выключи музыку", "Поставь джаз", "Пауза", "Продолжи книгу", "Найди Земфиру",
    "Что сейчас играет?", "что это играет", "Следующий трек", "сделай погромче", "Потише, пожалуйста", "Громче",
    "Прочитай, что в окне", "Зачитай выделенное", "читай дальше", "Что на экране?", "Опиши экран",
    "Посмотри, есть ли новые сообщения", "Увеличь", "уменьши лупу", "Прочитай скопированный текст",
    "Убавь звук", "смени станцию", "Стоп", "читай медленнее",
])
def test_action_gets_budget(text):
    assert budget(text) == ACTION


@pytest.mark.parametrize("text", [
    "Привет, как дела?", "Расскажи анекдот", "Мне сегодня грустно", "Ты меня слышишь?",
    "Какая тишина вокруг", "Экранизация была лучше книги", "Стопка книг",
])
def test_chat_has_no_budget(text):
    assert budget(text) == CHAT


def test_every_pattern_compiles_and_is_lowercase():
    import re
    for p in core.ACTION_PATTERNS:
        re.compile(p)
        assert p == p.lower()
