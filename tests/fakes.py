"""Подделки сети и процессов для тестов."""
import json


class FakeContent:
    """r.content у aiohttp: итерация по строкам и iter_chunked."""

    def __init__(self, lines=(), chunks=(), error=None):
        self._lines = list(lines)
        self._chunks = list(chunks)
        self._error = error

    def __aiter__(self):
        return self._lines_gen()

    async def _lines_gen(self):
        for line in self._lines:
            yield line if isinstance(line, bytes) else line.encode("utf-8")
        if self._error:
            raise self._error

    async def _chunks_gen(self):
        for c in self._chunks:
            yield c
        if self._error:
            raise self._error

    def iter_chunked(self, n):
        return self._chunks_gen()


class FakeResponse:
    def __init__(self, status=200, lines=(), chunks=(), body="", error=None):
        self.status = status
        self.content = FakeContent(lines, chunks, error)
        self._body = body

    async def text(self):
        return self._body

    async def json(self, **kw):
        return json.loads(self._body)

    async def read(self):
        return self._body.encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """post() отдаёт заготовленные ответы по очереди; исключение в очереди — бросается при входе."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, **kw):
        self.requests.append((url, kw))
        resp = self.responses.pop(0)
        if isinstance(resp, BaseException):
            return _Raiser(resp)
        return resp


class _Raiser:
    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *exc):
        return False


def sse(delta=None, raw=None):
    """Строка потока OpenAI-совместимого сервера."""
    if raw is not None:
        return "data: " + raw + "\n"
    return "data: " + json.dumps({"choices": [{"delta": delta}]}, ensure_ascii=False) + "\n"
