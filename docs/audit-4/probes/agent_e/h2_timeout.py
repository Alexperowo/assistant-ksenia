import asyncio, os, aiohttp, time
from aiohttp import web
async def main():
    async def h(req):
        ws = web.WebSocketResponse(heartbeat=5); await ws.prepare(req)
        async for _ in ws: pass
        return ws
    app = web.Application(); app.add_routes([web.get("/w", h)])
    r = web.AppRunner(app); await r.setup(); site = web.TCPSite(r, "127.0.0.1", 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    print("aiohttp", aiohttp.__version__, "default ClientTimeout:", aiohttp.ClientSession().timeout)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as sess:
        ws = await sess.ws_connect(f"http://127.0.0.1:{port}/w", heartbeat=5)
        t0 = time.monotonic()
        try:
            m = await asyncio.wait_for(ws.receive(), 3.5)
            print("session total=1s: ws got", m.type, m.data, "after %.1fs" % (time.monotonic() - t0))
        except asyncio.TimeoutError:
            print("session total=1s: ws still open after 3.5 s")
    os._exit(0)
asyncio.run(main())
