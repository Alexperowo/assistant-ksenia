"""HTTP ядра и слуха: только локальные программы, не веб-страницы из браузера."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import core
import voice_in


async def ok_handler(request):
    return web.json_response({"ok": True})


def status(module, headers):
    req = make_mocked_request("POST", "/talk", headers=headers)
    return asyncio.run(module.local_only(req, ok_handler)).status


@pytest.mark.parametrize("module", [core, voice_in])
@pytest.mark.parametrize("headers", [
    {"Host": "127.0.0.1:18130"},                                     # curl, команда ksenia
    {"Host": "localhost:18130"},
    {"Host": "127.0.0.1:18130", "Origin": "http://127.0.0.1:18130"},  # своя страница (будущая PWA через ядро)
])
def test_local_clients_allowed(module, headers):
    assert status(module, headers) == 200


@pytest.mark.parametrize("module", [core, voice_in])
@pytest.mark.parametrize("headers", [
    {"Host": "127.0.0.1:18130", "Origin": "https://evil.example"},  # CSRF: страница шлёт POST на localhost
    {"Host": "127.0.0.1:18130", "Origin": "null"},                  # песочница/file://
    {"Host": "rebind.evil.example:18130"},                          # DNS rebinding
    {},
])
def test_browser_pages_rejected(module, headers):
    assert status(module, headers) == 403
