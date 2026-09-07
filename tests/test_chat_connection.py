import contextlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request

from butler.chat import ChatError, _open_completion_response, _reader_counts, complete_chat
from butler.tasking import TaskCancelled


@contextlib.contextmanager
def local_model(mode):
    entered = threading.Event()
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            entered.set()
            if mode == 'fragmented':
                try:
                    for part in (b'HTTP/1.1 20', b'0 OK\r\nConnection: cl', b'ose\r\n\r\n'):
                        self.wfile.write(part)
                        self.wfile.flush()
                        release.wait(0.08)
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
                except OSError:
                    pass
                return
            if mode == 'stalled':
                release.wait(2)
            try:
                self.send_response(503 if mode == 'error' else 302 if mode == 'redirect' else 200)
                if mode == 'redirect':
                    self.send_header('Location', 'http://127.0.0.1:1/private')
                self.end_headers()
                if mode == 'error':
                    release.wait(2)
                else:
                    chunk = {'choices': [{'delta': {'content': 'Готово.'}, 'finish_reason': 'stop'}]}
                    self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                    self.wfile.flush()
                    if mode == 'body_stalled':
                        release.wait(2)
                    self.wfile.write(b'data: [DONE]\n\n')
            except (OSError, BrokenPipeError):
                pass

        def log_message(self, *_args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    worker.start()
    with tempfile.TemporaryDirectory() as directory:
        settings = SimpleNamespace(
            host='127.0.0.1', port=server.server_port,
            runtime_dir=Path(directory), raw={'diagnostics': {'enabled': False}},
        )
        try:
            yield settings, entered, requests
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            if worker.is_alive():
                raise AssertionError('Owned test HTTP server did not stop')


class CompletionConnectionTests(unittest.TestCase):
    def test_cancel_while_waiting_for_headers_stops_transport_worker(self):
        # Repeated real sockets, not a mock urlopen. Old code waits two seconds
        # for the server's headers before consulting checkpoint.
        for _ in range(10):
            with local_model('stalled') as (settings, entered, requests):
                def checkpoint():
                    if entered.is_set():
                        raise TaskCancelled('cancel header wait')
                started = time.monotonic()
                with self.assertRaises(TaskCancelled):
                    complete_chat(settings, [{'role': 'user', 'content': 'test'}], checkpoint=checkpoint)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(len(requests), 1)
                self.assertEqual(_reader_counts()['active_reader_threads'], 0)
                self.assertEqual(_reader_counts()['stuck_reader_threads'], 0)

    def test_pre_cancelled_completion_sends_no_request(self):
        with local_model('normal') as (settings, _entered, requests):
            def checkpoint():
                raise TaskCancelled('cancel before request')
            with self.assertRaises(TaskCancelled):
                complete_chat(settings, [], checkpoint=checkpoint)
            self.assertEqual(requests, [])

    def test_streamed_completion_ignores_environment_proxy(self):
        with local_model('normal') as (settings, _entered, requests), patch.dict(
            'os.environ', {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}
        ):
            result = complete_chat(settings, [], checkpoint=lambda: None)
            self.assertEqual(result['choices'][0]['message']['content'], 'Готово.')
            self.assertTrue(requests[0]['stream'])

    def test_http_error_does_not_wait_for_unbounded_error_body(self):
        with local_model('error') as (settings, _entered, _requests):
            started = time.monotonic()
            with self.assertRaisesRegex(ChatError, '503'):
                complete_chat(settings, [], checkpoint=lambda: None)
            self.assertLess(time.monotonic() - started, 1)

    def test_redirect_is_not_followed(self):
        with local_model('redirect') as (settings, _entered, _requests):
            with self.assertRaisesRegex(ChatError, '302'):
                complete_chat(settings, [], checkpoint=lambda: None)

    def test_partial_http_headers_survive_socket_poll_timeouts(self):
        with local_model('fragmented') as (settings, _entered, _requests):
            reply = complete_chat(settings, [], checkpoint=lambda: None)
            self.assertEqual(reply['choices'][0]['message']['content'], 'ok')

    def test_body_reader_is_also_cancelled_after_first_token(self):
        with local_model('body_stalled') as (settings, _entered, _requests):
            cancelled = threading.Event()
            def checkpoint():
                if cancelled.is_set():
                    raise TaskCancelled('cancel body')
            with self.assertRaises(TaskCancelled):
                complete_chat(settings, [], checkpoint=checkpoint, on_content_delta=lambda _text: cancelled.set())
            self.assertEqual(_reader_counts()['active_reader_threads'], 0)
            self.assertEqual(_reader_counts()['stuck_reader_threads'], 0)

    def test_header_deadline_reaps_transport_without_user_cancel(self):
        with local_model('stalled') as (settings, _entered, _requests):
            request = Request(f'http://127.0.0.1:{settings.port}/v1/chat/completions', data=b'{}')
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                _open_completion_response(request, checkpoint=lambda: None, timeout=0.2,
                                          diagnostics_source=settings, request_id='test-deadline')
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(_reader_counts()['active_reader_threads'], 0)

    def test_controlled_transport_rejects_remote_endpoint_before_socket_creation(self):
        with patch('butler.chat.socket.socket') as create_socket:
            with self.assertRaises(OSError):
                _open_completion_response(Request('http://example.test/private', data=b'{}'),
                                          checkpoint=lambda: None, timeout=1,
                                          diagnostics_source=None, request_id='test-remote')
            create_socket.assert_not_called()
