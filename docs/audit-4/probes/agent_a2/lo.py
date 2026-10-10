import asyncio, sys
sys.path.insert(0, "core"); sys.path.insert(0, "tests")
import conftest  # noqa
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import core
async def ok(r): return web.json_response({})
cases = [
 {"Host": "[::1]:18130"}, {"Host": "LOCALHOST"}, {"Host": "127.0.0.1:18130@evil.com"},
 {"Host": "evil.com#@127.0.0.1"}, {"Host": "127.0.0.1", "Origin": ""}, {"Host": "127.0.0.1", "Origin": "http://localhost."},
 {"Host": "127.0.0.1", "Origin": "http://127.0.0.1.evil.com"}, {"Host": "127.0.0.1", "X-Forwarded-For": "8.8.8.8", "X-Forwarded-Host": "evil"},
 {"Host": "127.0.0.1", "Origin": "http://[::1]:3000"}, {"Host": "127.0.0.1", "Origin": "http://127.0.0.1:8888"},
 {"Host": "0.0.0.0:18130"}, {"Host": "127.1"}, {"Host": "127.0.0.2"},
 {"Host": "127.0.0.1", "Upgrade": "websocket", "Connection": "Upgrade", "Origin": "https://evil.example"},
]
for h in cases:
    req = make_mocked_request("GET", "/client", headers=h)
    print(asyncio.run(core.local_only(req, ok)).status, h)
