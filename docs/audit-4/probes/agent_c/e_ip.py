import sys, asyncio
sys.path.insert(0, "/home/user/assistant-ksenia/core")
from tools import net_guard, web
cases = ["64:ff9b::7f00:1", "64:ff9b:1::a00:1", "2002:7f00:1::1", "2002:c0a8:101::1", "::7f00:1", "::ffff:0:7f00:1",
         "2001:0:4136:e378:8000:63bf:3fff:fdd2", "192.0.0.170", "198.18.0.1", "100.64.0.1", "255.255.255.255",
         "fec0::1", "::", "0.1.2.3", "240.0.0.1", "192.88.99.1"]
for c in cases:
    try:
        print(f"{c:40s} guard_public={net_guard.ip_is_public(c)}")
    except Exception as e:
        print(c, "ERR", e)
async def main():
    for h in ["127.1", "0x7f.1", "0177.0.0.1", "2130706433", "0x7f000001", "127.0.0.1.nip.io", "[::ffff:7f00:1]", "localhost.", "LOCALHOST", "0", "0.0.0.0"]:
        try:
            print(f"{h:25s} resolve_public={await net_guard.resolve_public(h, 80)!r}")
        except Exception as e:
            print(h, "ERR", repr(e))
asyncio.run(main())
for u in ["http://100.64.0.1/", "http://[64:ff9b::7f00:1]/", "http://198.18.0.1/", "http://127.1/", "http://2130706433/", "http://0/"]:
    print(f"{u:30s} web._public_url={web._public_url(u)}")
