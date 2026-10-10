"""/api/live probes: voice-in restart mid-call; core ends live conversation (/say from Да, /stop, idle)."""
import asyncio, json, os, sys, tempfile, time
sys.path.insert(0, "/home/user/assistant-ksenia/pwa")
import aiohttp
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
import gateway
PIN = "482913"

class Back:
    def __init__(self):
        self.calls, self.pushed, self.push_ws, self.close_push_after = [], 0, None, None
    async def any(self, req):
        self.calls.append((req.path, (await req.read())[:80]))
        return web.json_response({"ok": True})
    async def client(self, req):
        ws = web.WebSocketResponse(); await ws.prepare(req)
        await ws.send_json({"type": "hello", "busy": False, "confirm": None})
        async for _ in ws: pass
        return ws
    async def push(self, req):
        ws = web.WebSocketResponse(); await ws.prepare(req); self.push_ws = ws
        async for m in ws:
            self.pushed += 1
            if self.close_push_after and self.pushed >= self.close_push_after:
                await ws.close()   # voice-in restarts
                break
        return ws
    def app(self):
        a = web.Application()
        a.add_routes([web.get("/client", self.client), web.get("/push", self.push), web.route("*", "/{t:.*}", self.any)])
        return a

async def setup():
    b = Back(); s = TestServer(b.app(), host="127.0.0.1"); await s.start_server()
    d = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(__file__)))
    open(os.path.join(d, "pin"), "w").write(PIN)
    gw = gateway.Gateway({"data_dir": d, "core_url": f"http://127.0.0.1:{s.port}", "voice_in_url": f"http://127.0.0.1:{s.port}"})
    c = TestClient(TestServer(gw.app(), host="127.0.0.1")); await c.start_server()
    o = {"Origin": f"https://127.0.0.1:{c.port}"}
    r = await c.post("/api/login", json={"pin": PIN}, headers=o)
    return b, gw, c, {**o, "Cookie": f"{gateway.COOKIE}={r.cookies[gateway.COOKIE].value}"}

async def voice_in_restart():
    b, gw, c, h = await setup()
    b.close_push_after = 3
    live = await c.ws_connect("/api/live", headers=h)
    print("first msg:", await live.receive_json(timeout=2))
    for i in range(10):
        await live.send_bytes(b"\0" * 640); await asyncio.sleep(0.05)
    try:
        m = await live.receive(timeout=1.5)
        print("tablet got after voice-in closed /push:", m.type, m.data)
    except asyncio.TimeoutError:
        print("tablet got NOTHING after voice-in closed /push; live ws closed?", live.closed, "; frames voice-in received:", b.pushed)

async def core_ends_live():
    b, gw, c, h = await setup()
    live = await c.ws_connect("/api/live", headers=h)
    await live.receive_json(timeout=2)
    await asyncio.sleep(0.1)
    # the tablet taps «Да» on the confirm card (core /say with output=client runs stop_conversation())
    await c.post("/api/text", json={"text": "да"}, headers=h)
    await c.post("/api/stop", json={}, headers=h)  # «Стоп» button -> core /stop -> stop_conversation()
    await asyncio.sleep(0.2)
    n0 = b.pushed
    for i in range(5):
        await live.send_bytes(b"\0" * 640)
    await asyncio.sleep(0.2)
    try:
        m = await live.receive(timeout=1.0); print("tablet live ws got:", m.type, m.data)
    except asyncio.TimeoutError:
        print("after core /say + /stop: tablet live ws open =", not live.closed, "; still streaming to voice-in:", b.pushed - n0, "frames; core calls:", [x[0] for x in b.calls])

async def main():
    for t in sys.argv[1:]:
        print("==", t); await globals()[t]()
    os._exit(0)
asyncio.run(main())
