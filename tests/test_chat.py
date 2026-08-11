import json
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from butler.chat import (
    SentenceChunker,
    _read_complete_stream,
    complete_chat,
    count_chat_tokens,
    default_max_tokens,
    normalize_system_messages,
    stream_chat,
)


class _FakeResponse:
    def __init__(self, value=None, *, lines=()):
        self.value = value
        self.lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode("utf-8")

    def __iter__(self):
        return iter(self.lines)


class SentenceChunkerTests(unittest.TestCase):
    def test_strict_templates_receive_one_leading_system_message(self):
        normalized = normalize_system_messages(
            [
                {"role": "system", "content": "Основные правила."},
                {"role": "user", "content": "Задача"},
                {"role": "assistant", "content": "Промежуточный ответ"},
                {"role": "system", "content": "Теперь дай итог."},
            ]
        )
        self.assertEqual(
            [message["role"] for message in normalized],
            ["system", "user", "assistant"],
        )
        self.assertEqual(
            normalized[0]["content"],
            "Основные правила.\n\nТеперь дай итог.",
        )

    def test_releases_complete_sentence_from_stream(self):
        chunker = SentenceChunker(minimum_length=5)
        self.assertEqual(chunker.feed("Здравствуйте, "), [])
        self.assertEqual(
            chunker.feed("Александр. Чем могу помочь? "),
            ["Здравствуйте, Александр.", "Чем могу помочь?"],
        )
        self.assertEqual(chunker.finish(), "")

    def test_finish_releases_short_tail(self):
        chunker = SentenceChunker()
        chunker.feed("Хорошо")
        self.assertEqual(chunker.finish(), "Хорошо")

    def test_default_response_budget_is_4096(self):
        self.assertEqual(default_max_tokens(SimpleNamespace(raw={})), 4096)
        self.assertEqual(
            default_max_tokens(SimpleNamespace(raw={"generation": {"max_tokens": 5000}})),
            5000,
        )

    def test_cancellable_stream_reassembles_tool_call(self):
        events = [
            {
                "choices": [{"delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "browser_", "arguments": "{\"query\":"},
                }]}}]
            },
            {
                "choices": [{
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "function": {"name": "search", "arguments": "\"тест\"}"},
                    }]},
                    "finish_reason": "tool_calls",
                }]
            },
        ]
        response = [
            ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            for event in events
        ] + [b"data: [DONE]\n"]
        checkpoints = []
        value = _read_complete_stream(response, lambda: checkpoints.append(True))
        call = value["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "browser_search")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"query": "тест"})
        self.assertGreaterEqual(len(checkpoints), 3)


class ChatTransportTests(unittest.TestCase):
    def _settings(self, runtime_dir: Path):
        return SimpleNamespace(
            host="127.0.0.1",
            port=18080,
            runtime_dir=runtime_dir,
            raw={"diagnostics": {"enabled": False}, "generation": {"max_tokens": 4096}},
        )

    @staticmethod
    def _messages():
        return [
            {"role": "system", "content": "Основные правила."},
            {"role": "user", "content": "Задача"},
            {"role": "system", "content": "Только итог."},
        ]

    def test_complete_transport_sends_one_leading_system_message(self):
        response = _FakeResponse(
            {"choices": [{"message": {"role": "assistant", "content": "Готово."}}]}
        )
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch("butler.chat.urllib.request.urlopen", return_value=response) as urlopen:
                result = complete_chat(settings, self._messages())

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result["choices"][0]["message"]["content"], "Готово.")
        self.assertEqual([item["role"] for item in payload["messages"]], ["system", "user"])
        self.assertEqual(
            payload["messages"][0]["content"],
            "Основные правила.\n\nТолько итог.",
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 600)

    def test_tokenizer_transport_normalizes_the_same_messages(self):
        response = _FakeResponse({"tokens": [1, 2, 3]})
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch("butler.chat.urllib.request.urlopen", return_value=response) as urlopen:
                count = count_chat_tokens(settings, self._messages())

        request = urlopen.call_args.args[0]
        outer = json.loads(request.data.decode("utf-8"))
        sent_messages = json.loads(outer["content"])
        self.assertEqual(count, 3)
        self.assertEqual([item["role"] for item in sent_messages], ["system", "user"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)

    def test_stream_transport_normalizes_the_same_messages(self):
        event = {
            "choices": [{"delta": {"content": "Ответ"}, "finish_reason": None}]
        }
        response = _FakeResponse(
            lines=[
                ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"),
                b"data: [DONE]\n",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch("butler.chat.urllib.request.urlopen", return_value=response) as urlopen:
                chunks = list(stream_chat(settings, self._messages()))

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(chunks, ["Ответ"])
        self.assertEqual([item["role"] for item in payload["messages"]], ["system", "user"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 600)


if __name__ == "__main__":
    unittest.main()
