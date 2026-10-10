import sys, asyncio, importlib.util
spec = importlib.util.spec_from_file_location("unblock", "/home/user/assistant-ksenia/net/unblock.py")
ub = importlib.util.module_from_spec(spec); spec.loader.exec_module(ub)
asked = []
async def fake_socks(r, w):
    await r.readexactly(3); w.write(b"\x05\x00"); await w.drain()
    head = await r.readexactly(5); host = await r.readexactly(head[4]); port = int.from_bytes(await r.readexactly(2), "big")
    asked.append(f"{host.decode()}:{port}")
    w.write(b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00"); await w.drain(); w.close()
async def main():
    s = await asyncio.start_server(fake_socks, "127.0.0.1", 0); ub.SOCKS_PORT = s.sockets[0].getsockname()[1]
    b = await asyncio.start_server(ub.handle, "127.0.0.1", 0); bport = b.sockets[0].getsockname()[1]
    for t in ["127.0.0.1:18100", "192.168.1.1:80", "[::1]:18130", "169.254.169.254:80"]:
        r, w = await asyncio.open_connection("127.0.0.1", bport)
        w.write(f"CONNECT {t} HTTP/1.1\r\nHost: {t}\r\n\r\n".encode()); await w.drain()
        print(f"CONNECT {t:22s} ->", (await asyncio.wait_for(r.readline(), 3)).strip())
        w.close()
    print("bridge asked byedpi to connect to:", asked)
    print("listen sockets:", b.sockets[0].getsockname(), "| byedpi argv:", [ub.CIADPI, "-i", "127.0.0.1", "-p", "10801", "..."])
asyncio.run(main())
