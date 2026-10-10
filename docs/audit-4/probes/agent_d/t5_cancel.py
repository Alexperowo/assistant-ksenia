import sys, asyncio, time, os; sys.path.insert(0, "/home/user/assistant-ksenia/core"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest, numpy as np
import core
from fakes import FakeResponse, FakeSession
HERE = os.path.dirname(os.path.abspath(__file__)); LOG = os.path.join(HERE, "pacat.log")
core.CONFIG.update({"record_replies": False, "night_from": 24, "night_to": 0})
core.pick_output_sink = lambda: None
real_exec = asyncio.create_subprocess_exec
async def fake_exec(*args, **kw):
    assert args[0] == "pacat"
    return await real_exec(sys.executable, os.path.join(HERE, "fake_pacat.py"), LOG, **kw)
asyncio.create_subprocess_exec = fake_exec
loud = (np.ones(44100 * 10, dtype=np.int16) * 10000).tobytes()
class Content:
    async def iter_chunked(self, n):
        for i in range(0, len(loud), 8192):
            await asyncio.sleep(8192 / 88200 / 4)   # TTS 4x faster than real time
            yield loud[i:i + 8192]
class Resp(FakeResponse):
    def __init__(self): super().__init__(200); self.content = Content()
async def main(stop_at):
    open(LOG, "w").close()
    sp = core.Speaker(FakeSession(Resp()))
    task = asyncio.create_task(sp.speak("Длинная фраза.", {"_t0": time.time()}))
    await asyncio.sleep(stop_at)
    t_cancel = time.time(); qlen = sp._q.qsize()
    await sp.cancel(); t_done = time.time()
    await task
    await asyncio.sleep(1.0)
    rows = [l.split() for l in open(LOG) if l.strip() and l.strip() != "EOF"]
    after = [(float(t) - t_cancel, int(a)) for t, a in rows if float(t) > t_cancel]
    full = [a for dt, a in after if a > 9500]; fade = [a for dt, a in after if 100 < a <= 9500]
    print(f"stop at {stop_at}s: queue items at cancel={qlen}; cancel() took {t_done - t_cancel:.2f}s; "
          f"audio played after cancel: {len(after)*0.02:.2f}s (full-level {len(full)*0.02:.2f}s, fade blocks {len(fade)}); "
          f"last block at +{after[-1][0] if after else 0:.2f}s, 'EOF' (stdin drained) reached: {'EOF' in open(LOG).read()}")
for s in (1.0, 3.0):
    asyncio.run(main(s))
