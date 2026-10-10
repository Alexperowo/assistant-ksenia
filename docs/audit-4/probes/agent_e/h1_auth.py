"""Auth / local-port / timeout probes against pwa/gateway.py with fake upstreams (scratch only)."""
import asyncio, json, os, socket, sys, tempfile
sys.path.insert(0, "/home/user/assistant-ksenia/pwa")
import aiohttp
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
import gateway

PIN = "482913"


def mkdata():
    d = tempfile.mkdtemp(prefix="gw_", dir=os.path.dirname(os.path.abspath(__file__)))
    open(os.path.join(d, "pin"), "w").write(PIN + "\n")
    json.dump(["192.168.0.14", "ksenia-pc.local", "ksenia-pc"], open(os.path.join(d, "hosts.json"), "w"))
    return d


class Core:
    def __init__(self):
        self.calls = []
        self.ws = []
        self.down = False

    async def any(self, req):
        body = await req.read()
        self.calls.append((req.method, req.path, body[:200]))
        return web.json_response({"ok": True, "say": "ok"})

    async def client(self, req):
        if self.down:
            return web.Response(status=503)
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws.append(ws)
        await ws.send_json({"type": "hello", "busy": False, "confirm": None})
        async for _ in ws:
            pass
        return ws

    def app(self):
        a = web.Application()
        a.add_routes([web.get("/client", self.client), web.route("*", "/{tail:.*}", self.any)])
        return a


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


async def setup(local=False, **extra):
    core = Core()
    cs = TestServer(core.app(), host="127.0.0.1"); await cs.start_server()
    port = free_port()
    cfg = {"data_dir": mkdata(), "core_url": f"http://127.0.0.1:{cs.port}", "voice_in_url": f"http://127.0.0.1:{cs.port}",
           **extra}
    if local:
        cfg["local_port"] = port
    gw = gateway.Gateway(cfg)
    c = TestClient(TestServer(gw.app(), host="127.0.0.1", port=port)); await c.start_server()
    return core, cs, gw, c


async def t_global_lockout():
    """Attacker on 4 LAN IPs x 5 bad PINs -> owner with CORRECT pin from a 5th IP is refused for ~1h."""
    core, cs, gw, c = await setup()
    cur = {"ip": "192.168.0.50"}
    gateway.client_ip = lambda request: cur["ip"]
    o = {"Origin": f"https://127.0.0.1:{c.port}"}
    for i in range(20):
        cur["ip"] = f"192.168.0.{50 + i // 5}"
        await c.post("/api/login", json={"pin": f"{i:06d}"}, headers=o)
    cur["ip"] = "192.168.0.77"  # the real tablet
    r = await c.post("/api/login", json={"pin": PIN}, headers=o)
    print("global lockout: owner with correct PIN ->", r.status, await r.json(), "Retry-After", r.headers.get("Retry-After"))
    await c.close(); await cs.close()


async def t_local_port_host():
    """Local site (no login) accepts LAN DNS names in Host -> DNS/mDNS rebinding to 127.0.0.1 gets full control."""
    core, cs, gw, c = await setup(local=True)
    h = {"Host": f"ksenia-pc.local:{c.port}", "Origin": f"http://ksenia-pc.local:{c.port}"}
    r = await c.post("/api/control/act", json={"action": "restart", "unit": "ksenia-core"}, headers=h)
    print("local port, Host=ksenia-pc.local, no cookie ->", r.status, await r.text(), "core got:", core.calls[-1:])
    r = await c.get("/api/control/state", headers=h)
    print("local port GET state ->", r.status)
    h2 = {"Host": f"evil.example:{c.port}", "Origin": f"http://evil.example:{c.port}"}
    r = await c.post("/api/control/act", json={"action": "stop"}, headers=h2)
    print("local port, Host=evil.example ->", r.status)
    await c.close(); await cs.close()


async def t_ws_after_logout():
    """Revoked session keeps its open /api/ws (and /api/live) streams."""
    core, cs, gw, c = await setup()
    o = {"Origin": f"https://127.0.0.1:{c.port}"}
    r = await c.post("/api/login", json={"pin": PIN}, headers=o)
    tok = r.cookies[gateway.COOKIE].value
    hdr = {**o, "Cookie": f"{gateway.COOKIE}={tok}"}
    for _ in range(50):
        if gw.core_up: break
        await asyncio.sleep(0.02)
    ws = await c.ws_connect("/api/ws", headers=hdr)
    await ws.receive_json()
    await c.post("/api/logout", headers=hdr)
    st = (await c.post("/api/text", json={"text": "x"}, headers=hdr)).status
    await core.ws[0].send_json({"type": "say", "text": "секрет после выхода"})
    m = await ws.receive_json(timeout=2)
    print("after logout: REST ->", st, "; open ws still receives:", m)
    await ws.close(); await c.close(); await cs.close()


async def t_stale_confirm():
    """Core goes down while a confirm is pending: gateway keeps replaying it to new tabs."""
    core, cs, gw, c = await setup()
    o = {"Origin": f"https://127.0.0.1:{c.port}"}
    r = await c.post("/api/login", json={"pin": PIN}, headers=o)
    hdr = {**o, "Cookie": f"{gateway.COOKIE}={r.cookies[gateway.COOKIE].value}"}
    for _ in range(50):
        if gw.core_up: break
        await asyncio.sleep(0.02)
    await core.ws[0].send_json({"type": "confirm", "label": "x", "question": "Пишу Диме: привет. Отправить?"})
    await asyncio.sleep(0.1)
    core.down = True
    await core.ws[0].close()  # core dies (restart)
    await asyncio.sleep(0.3)
    print("gw.core_up =", gw.core_up)
    ws = await c.ws_connect("/api/ws", headers=hdr)
    a = await ws.receive_json(timeout=2); b = await ws.receive_json(timeout=2)
    print("core down, new tab gets:", a, b)
    os._exit(0)


async def t_session_default_timeout():
    """aiohttp ClientSession default timeout (total=300 s) vs long-lived ws_connect (core /client, voice-in /push)."""
    async def h(req):
        ws = web.WebSocketResponse(heartbeat=1); await ws.prepare(req)
        async for _ in ws: pass
        return ws
    app = web.Application(); app.add_routes([web.get("/w", h)])
    s = TestServer(app, host="127.0.0.1"); await s.start_server()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as sess:
        ws = await sess.ws_connect(f"http://127.0.0.1:{s.port}/w", heartbeat=1)
        t0 = asyncio.get_running_loop().time()
        try:
            m = await ws.receive(timeout=3.5)
            print("ws with session total=1s: got", m.type, "after %.1fs" % (asyncio.get_running_loop().time() - t0))
        except asyncio.TimeoutError:
            print("ws with session total=1s: still open after 3.5 s (session total timeout does NOT cut ws)")
        await ws.close()
    await s.close()


async def main():
    for t in sys.argv[1:]:
        print("==", t)
        await globals()[t]()
    os._exit(0)

asyncio.run(main())
