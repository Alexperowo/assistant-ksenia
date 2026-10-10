import sys, asyncio, logging; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest
import numpy as np, voice_in
from aiohttp import web, test_utils
logging.basicConfig(level=logging.ERROR)
RATE=16000
def pcm(*p): return (np.clip(np.concatenate(p), -1, 1)*32767).astype(np.int16).tobytes()
speech = lambda s: 0.1*np.sin(2*np.pi*220*np.arange(int(RATE*s))/RATE)
zeros = lambda s: np.zeros(int(RATE*s))

class FakeRec:
    def __init__(s, data): s.data, s.pos, s.returncode, s.killed = data, 0, None, False; s.stdout = s
    async def readexactly(s, n):
        await asyncio.sleep(0.001)
        if s.pos + n > len(s.data): await asyncio.sleep(3600)
        c = s.data[s.pos:s.pos+n]; s.pos += n; return c
    def kill(s): s.killed = True; s.returncode = -9
    async def wait(s): return s.returncode

class Ear:
    lock = asyncio.Lock(); streaming = False; vp = None; turn = None; model = object()
    def transcribe(self, pcm): raise RuntimeError("CUDA failure 999: unknown error")
    def turn_check(self, pcm): return 1.0, self.transcribe(pcm)
    def transcribe_sure(self, pcm): return self.transcribe(pcm), None
    _save_debug = staticmethod(lambda f: None)

async def main():
    voice_in.ear = Ear(); voice_in.headset = None
    voice_in.CONFIG = {**voice_in.CONFIG, "beep": False, "le_settle_s": 0}
    voice_in.find_bt_card = lambda: "bluez_card.88_92"
    voice_in.card_profiles = lambda c: ["bap-duplex"]
    voice_in.card_profile = lambda c: "bap-duplex"
    voice_in.find_source = lambda c: "bluez_input.x"
    voice_in.bt_node = lambda c, k: None
    rec = FakeRec(pcm(zeros(0.5), speech(0.8), zeros(3)))
    async def fake_exec(*a, **k): return rec
    asyncio.create_subprocess_exec = fake_exec
    app = web.Application(); app.add_routes([web.get("/stream", voice_in.handle_stream)])
    async with test_utils.TestServer(app) as srv, test_utils.TestClient(srv) as cl:
        ws = await cl.ws_connect("/stream")
        msgs = []
        while True:
            m = await ws.receive(timeout=10)
            msgs.append((m.type.name, m.data if m.type.name == "TEXT" else m.extra))
            if m.type.name != "TEXT": break
        print("client received:", msgs)
        print("parec killed:", rec.killed, " ear.streaming after:", voice_in.ear.streaming)
asyncio.run(main())
