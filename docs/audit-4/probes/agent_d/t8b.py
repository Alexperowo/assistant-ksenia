import sys, asyncio, logging, time; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest, numpy as np, voice_in, aiohttp
from aiohttp import web
print("imported", flush=True)
real_exec = asyncio.create_subprocess_exec
spawned = []
async def fake_exec(*args, **kw):
    print("exec", args[0], flush=True)
    if args[0] == "pacat":
        p = await real_exec(sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(3600)", **kw)
        spawned.append(p); return p
    raise AssertionError(args)
class Ear:
    lock = asyncio.Lock(); streaming = False; vp = None
async def main():
    voice_in.ear = Ear(); voice_in.ear.record_utterance = voice_in.Ear.record_utterance.__get__(voice_in.ear)
    voice_in.CONFIG = {**voice_in.CONFIG, "bluetooth": False, "source": "mic", "beep_sink": "bluez_output.X", "beep": True}
    asyncio.create_subprocess_exec = fake_exec
    app = web.Application(); app.add_routes([web.post("/listen", voice_in.handle_listen)])
    runner = web.AppRunner(app); await runner.setup(); site = web.TCPSite(runner, "127.0.0.1", 18999); await site.start()
    print("server up", flush=True)
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post("http://127.0.0.1:18999/listen", timeout=aiohttp.ClientTimeout(total=2)) as r:
                print("1st", r.status, flush=True)
        except Exception as e:
            print("1st /listen: client gave up:", type(e).__name__, flush=True)
        async with s.post("http://127.0.0.1:18999/listen", timeout=aiohttp.ClientTimeout(total=5)) as r:
            print("2nd /listen ->", r.status, await r.text(), "| ear.lock held:", voice_in.ear.lock.locked(), flush=True)
    for p in spawned: p.kill()
    await asyncio.sleep(0.2)
    print("after killing the stuck pacat, lock held:", voice_in.ear.lock.locked(), flush=True)
    import os; os._exit(0)
asyncio.run(main())
