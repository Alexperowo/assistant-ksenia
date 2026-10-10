import sys, asyncio, logging; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest, numpy as np, voice_in, aiohttp
from aiohttp import web, test_utils
logging.basicConfig(level=logging.CRITICAL)
real_exec = asyncio.create_subprocess_exec
spawned = []
async def fake_exec(*args, **kw):
    if args[0] == "pacat":   # sink whose Bluetooth transport never starts: pacat accepts input but never finishes
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
    async with test_utils.TestServer(app) as srv, test_utils.TestClient(srv) as cl:
        try:
            await cl.post("/listen", timeout=aiohttp.ClientTimeout(total=2))
        except asyncio.TimeoutError:
            print("1st /listen: client gave up after 2 s (core gives up after 90 s)")
        await asyncio.sleep(1)
        r = await cl.post("/listen"); print("2nd /listen ->", r.status, await r.text(), "| lock still held:", voice_in.ear.lock.locked())
        for p in spawned: p.kill()
asyncio.run(main())
