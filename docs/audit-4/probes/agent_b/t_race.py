import asyncio, json, sys, time, os, tempfile
sys.path.insert(0, "core"); sys.path.insert(0, "tests")
import core
from tools import confirm
from test_core_respond import FakeSpeaker, call, script_steps
core.Speaker = FakeSpeaker
core.HISTORY_FILE = os.path.join(tempfile.mkdtemp(), "h.json")
class NoHub:
    def emit(self, *a, **k): pass
core.hub = NoHub()
k = core.Ksenia.__new__(core.Ksenia)
k.history, k.window_start, k.last_tag, k.speaker, k.session = [], 0, None, None, None
k.lock, k.last_turn_t = asyncio.Lock(), time.time()
done = []
async def tool(name, args, session):
    r = confirm.ask("сообщение ВКонтакте для Петя", lambda: _send(), question="Пишу Петя: длинный текст. Отправить?")
    k.speaker.cancelled = True   # Alexander starts talking while the tool runs (live mode cancel_turn / headset tap)
    return r
async def _send():
    done.append("SENT"); return {"ok": True}
core.run_tool = tool
FakeSpeaker.instances = []
script_steps(k, [("", [call("c1", "vk_send", "{}")]), ("x", [])])
asyncio.run(k.respond("напиши Пете", {"_t0": time.time()}))
print("turn1 spoken:", FakeSpeaker.instances[0].spoken, "| pending:", bool(confirm.current()))
FakeSpeaker.instances = []
script_steps(k, [("Готово.", [])])
asyncio.run(k.respond("давай быстрее", {"_t0": time.time()}))
print("turn2 'давай быстрее' -> executed:", done)
