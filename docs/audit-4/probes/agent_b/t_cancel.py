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
log = []
async def send():
    log.append("typing"); await asyncio.sleep(2); log.append("clicked send"); return {"ok": True}
confirm.ask("сообщение ВКонтакте для Петя", send, question="Пишу Петя: привет. Отправить?")
script_steps(k, [("Отправила.", [])])
async def main():
    t = asyncio.create_task(k.respond("да", {"_t0": time.time()}))
    await asyncio.sleep(0.3); t.cancel()          # live barge-in: cancel_turn() while the core executes the action
    try: await t
    except asyncio.CancelledError: print("turn cancelled")
    script_steps(k, [("Что-что?", [])])
    await k.respond("ну что там", {"_t0": time.time()})
asyncio.run(main())
print("action log:", log, "| pending:", confirm.current())
print("history user msgs:", [m["content"][:60] for m in k.history if m["role"] == "user"])
