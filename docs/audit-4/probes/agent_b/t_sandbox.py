import asyncio, sys, time, os, tempfile
sys.path.insert(0, "core"); sys.path.insert(0, "tests")
import core
from tools import confirm
from test_core_respond import FakeSpeaker, script_steps
core.Speaker = FakeSpeaker
core.HISTORY_FILE = os.path.join(tempfile.mkdtemp(), "h.json")
class NoHub:
    def emit(self, *a, **k): pass
core.hub = NoHub()
k = core.Ksenia.__new__(core.Ksenia)
k.history, k.window_start, k.last_tag, k.speaker, k.session = [], 0, None, None, None
k.lock, k.last_turn_t = asyncio.Lock(), time.time()
core.ks = k
async def send(): return {"ok": True}
confirm.ask("сообщение ВКонтакте для Петя", send, question="Пишу Петя: привет. Отправить?")
script_steps(k, [("Сейчас 18:00.", [])])
asyncio.run(core.turn("Который час?", {"_t0": time.time()}, sandbox=True))
print("Alexander's pending action after a sandbox self-test phrase:", confirm.current())
