import asyncio, os, sys, json, ssl, subprocess
sys.path.insert(0, "/home/user/assistant-ksenia/pwa")
import gateway, aiohttp
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certwork/data/pwa")
open(os.path.join(D, "pin"), "w").write("482913")
cfg = {"https_port": 38140, "http_port": 38141, "local_port": 38142, "host": "0.0.0.0", "data_dir": D,
       "core_url": "http://127.0.0.1:1", "voice_in_url": "http://127.0.0.1:1"}
async def main():
    t = asyncio.create_task(gateway.serve(cfg))
    await asyncio.sleep(1.0)
    [print("LISTEN", l.split()[1]) for l in open("/proc/net/tcp").read().splitlines()[1:] if l.split()[1].split(":")[1] in ("94FC", "94FD", "94FE") and l.split()[3] == "0A"]
    # XFF: 6 bad PINs with changing X-Forwarded-For from one socket -> still per-IP locked
    ctx = ssl.create_default_context(cafile=os.path.join(D, "ca.crt"))
    async with aiohttp.ClientSession() as s:
        st = []
        for i in range(6):
            async with s.post("https://localhost:38140/api/login", json={"pin": f"{i:06d}"}, ssl=ctx,
                              headers={"Origin": "https://localhost:38140", "X-Forwarded-For": f"10.0.0.{i}", "X-Real-IP": f"10.0.0.{i}"}) as r:
                st.append(r.status)
        print("6 bad PINs with rotating X-Forwarded-For:", st)
        # IPv6 reachability of the HTTPS port
        try:
            async with s.get("https://[::1]:38140/", ssl=False, timeout=aiohttp.ClientTimeout(total=2)) as r:
                print("IPv6 ::1 -> HTTP", r.status)
        except Exception as e:
            print("IPv6 ::1 ->", type(e).__name__)
    os._exit(0)
asyncio.run(main())
