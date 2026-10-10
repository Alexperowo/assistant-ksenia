import sys, os, asyncio, time
sys.dont_write_bytecode = True
S = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, S + "/snap/core"); sys.path.insert(0, S + "/snap/tests")
import logging
logging.basicConfig(level=logging.ERROR, format="LOG %(levelname)s %(message)s", force=True)
import conftest, core
from fakes import FakeResponse
class Sess:
    def post(self, url, **kw): return FakeResponse(status=500, body="CUDA error: unspecified launch failure")
async def main():
    sp = core.Speaker(Sess(), output="local")
    sent = []
    sp._send = lambda c: sent.append(c)
    r = await sp.speak("[sigh] Я включилась, но не всё в порядке: голос не работает.", {"_t0": time.time()})
    print("speak returned", r, "| audio bytes sent:", sum(map(len, sent)), "| fallback voice used: none")
asyncio.run(main())
