import sys, asyncio; sys.path.insert(0, "/home/user/assistant-ksenia/core"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest, numpy as np, logging
import core
from fakes import FakeResponse
logging.disable(logging.CRITICAL)
core.CONFIG.update({"record_replies": False, "night_from": 24, "night_to": 0}); core.pick_output_sink = lambda: None
made = []
class Stdin:
    def __init__(s): s.data = bytearray(); s.closed = False
    def write(s, b): s.data.extend(b)
    async def drain(s): pass
    def close(s): s.closed = True
class Proc:
    def __init__(s): s.stdin = Stdin(); s.returncode = None
    def kill(s): s.returncode = -9
    async def wait(s): s.returncode = s.returncode if s.returncode is not None else 0; return s.returncode
async def fake_exec(*a, **k):
    p = Proc(); made.append(p); return p
asyncio.create_subprocess_exec = fake_exec
loud = (np.ones(44100, dtype=np.int16) * 10000).tobytes()
class SlowHeaders(FakeResponse):            # voice-out: first byte after ~0.7 s (normal first-audio latency)
    def __init__(s): super().__init__(200, chunks=[loud[i:i+8192] for i in range(0, len(loud), 8192)])
    async def __aenter__(s): await asyncio.sleep(0.7); return s
class Sess:
    def post(s, *a, **k): return SlowHeaders()
async def main():
    sp = core.Speaker(Sess())
    await sp.warm()                          # respond() warms the player before the first phrase
    t = asyncio.create_task(sp.speak("Следующая фраза.", {"_t0": 0}))
    await asyncio.sleep(0.2)
    await sp.cancel()                        # «стоп» while voice-out is still synthesizing
    print("after cancel: players spawned so far =", len(made))
    await t
    print("after the response arrived: players spawned =", len(made),
          "| bytes written to the post-cancel player =", len(made[-1].stdin.data) if len(made) > 1 else 0)
asyncio.run(main())
