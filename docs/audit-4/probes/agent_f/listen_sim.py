import sys, os, asyncio, json
sys.dont_write_bytecode = True
S = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, S + "/snap/core"); sys.path.insert(0, S + "/snap/tests")
import logging; logging.disable(logging.CRITICAL)
import conftest, core
from fakes import FakeResponse
class Sess:
    def post(self, url, **kw):  # voice-in whose ear.lock is held forever by a hung CUDA thread
        return FakeResponse(status=409, body=json.dumps({"error": "busy"}))
said = []
async def fake_notice(text, output=None): said.append(text)
async def main():
    core.ks.session = Sess()
    core.CONFIG["listen_busy_wait_s"] = 0.5
    core.say_notice = fake_notice
    async def noduck(*a, **k): pass
    core.music.duck = noduck
    for _ in range(3):
        await core.Conversation().run()
    print(said)
asyncio.run(main())
