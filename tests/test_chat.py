import json
import tempfile
import threading
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


class _BlockingResponse(_FakeResponse):
    def __init__(self):
        super().__init__(lines=())
        self.reader_started = threading.Event()
        self.release_reader = threading.Event()

    def __exit__(self, _type, _value, _traceback):
        self.release_reader.set()
        return False

    def __iter__(self):
        self.reader_started.set()
        self.release_reader.wait(2)
        return iter(())

    def read(self):
        self.reader_started.set()
        self.release_reader.wait(2)
        return b"{}"


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

    def test_short_complete_sentence_is_streamed_without_waiting_for_next_text(self):
        chunker = SentenceChunker(minimum_length=24)

        self.assertEqual(chunker.feed("Да, нашла. "), ["Да, нашла."])
        self.assertEqual(chunker.finish(), "")

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

    def test_complete_stream_forwards_content_deltas_in_order(self):
        events = [
            {"choices": [{"delta": {"content": "Первая"}}]},
            {"choices": [{"delta": {"content": " вторая"}}]},
        ]
        response = [
            ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            for event in events
        ]
        deltas = []

        value = _read_complete_stream(
            response,
            lambda: None,
            on_content_delta=deltas.append,
        )

        self.assertEqual(deltas, ["Первая", " вторая"])
        self.assertEqual(
            value["choices"][0]["message"]["content"], "Первая вторая"
        )


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

    def test_tokenizer_cancellation_is_observed_while_response_is_stalled(self):
        response = _BlockingResponse()
        checkpoint_calls = 0

        def checkpoint() -> None:
            nonlocal checkpoint_calls
            checkpoint_calls += 1
            if checkpoint_calls >= 3:
                raise RuntimeError("tokenizer cancelled")

        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch("butler.chat.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "tokenizer cancelled"):
                    count_chat_tokens(
                        settings,
                        self._messages(),
                        checkpoint=checkpoint,
                    )

        self.assertTrue(response.reader_started.is_set())
        self.assertTrue(response.release_reader.is_set())

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

    def test_stream_checkpoint_stops_before_next_delta(self):
        events = [
            {"choices": [{"delta": {"content": "Первый"}}]},
            {"choices": [{"delta": {"content": " второй"}}]},
        ]
        response = _FakeResponse(
            lines=[
                ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode(
                    "utf-8"
                )
                for event in events
            ]
        )
        cancelled = threading.Event()

        def checkpoint() -> None:
            if cancelled.is_set():
                raise RuntimeError("cancelled")

        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch("butler.chat.urllib.request.urlopen", return_value=response):
                stream = stream_chat(
                    settings,
                    self._messages(),
                    checkpoint=checkpoint,
                )
                self.assertEqual(next(stream), "Первый")
                cancelled.set()
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    next(stream)

    def test_stream_cancellation_is_observed_while_http_reader_is_stalled(self):
        response = _BlockingResponse()
        checkpoint_calls = 0

        def checkpoint() -> None:
            nonlocal checkpoint_calls
            checkpoint_calls += 1
            if checkpoint_calls >= 3:
                raise RuntimeError("cancelled while stalled")

        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            with patch("butler.chat.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "while stalled"):
                    list(
                        stream_chat(
                            settings,
                            self._messages(),
                            checkpoint=checkpoint,
                        )
                    )

        self.assertTrue(response.reader_started.is_set())
        self.assertTrue(response.release_reader.is_set())


if __name__ == "__main__":
    unittest.main()
