import asyncio, json, sys, time, os, tempfile
sys.path.insert(0, "core"); sys.path.insert(0, "tests")
import core
from tools import confirm, memory, daily, desktop
from test_core_respond import FakeSpeaker, call, script_steps
core.Speaker = FakeSpeaker
tmp = tempfile.mkdtemp()
core.HISTORY_FILE = os.path.join(tmp, "h.json")
memory.FILE = os.path.join(tmp, "mem.json")
daily.FILE = os.path.join(tmp, "rem.json")
if hasattr(core, "prefix_file"):
    pass

class NoHub:
    def emit(self, *a, **k): pass
core.hub = NoHub()

def mk():
    k = core.Ksenia.__new__(core.Ksenia)
    k.history, k.window_start, k.last_tag, k.speaker, k.session = [], 0, None, None, None
    k.lock, k.last_turn_t = asyncio.Lock(), time.time()
    return k

executed = []
def action(tag):
    async def run():
        executed.append(tag); return {"ok": True}
    return run

async def tool_vk(name, args, session):
    a = json.loads(args) if isinstance(args, str) else args
    return confirm.ask(f"сообщение ВКонтакте для {a['to']}", action("SEND to " + a['to']),
                       question=f"Пишу {a['to']}: {a['text']}. Отправить?")

def run(k, text, steps, **kw):
    FakeSpeaker.instances = []
    script_steps(k, steps)
    asyncio.run(k.respond(text, {"_t0": time.time()}, **kw))
    return [s for sp in FakeSpeaker.instances for s in sp.spoken]

# ---- A: question, then a reminder (internal) turn ending with a question, then "ага" ----
k = mk(); orig = core.run_tool; core.run_tool = tool_vk
print("A1 spoken:", run(k, "напиши Пете привет", [("", [call("c1", "vk_send", json.dumps({"to": "Петя", "text": "привет"}))]), ("Жду.", [])]))
print("A2 internal spoken:", run(k, core.reminder_prompt({"text": "выпить таблетки", "ts": time.time()}), [("Пора выпить таблетки. Выпил?", [])], internal=True))
print("A3 pending before 'ага':", bool(confirm.current()))
run(k, "ага", [("Хорошо.", [])])
print("A RESULT executed:", executed)
print("A last user note:", k.history[-2]["content"][-120:] if k.history[-2]["role"]=="user" else k.history[-2])

# ---- B: short 'да' from unknown voice (voiceprint returns owner None for <0.6 s) ----
executed.clear(); confirm.cancel(); k = mk()
run(k, "напиши Пете привет", [("", [call("c1", "vk_send", json.dumps({"to": "Петя", "text": "привет"}))]), ("Жду.", [])])
run(k, "да", [("Ок.", [])], speaker={"owner": None, "enrolled": True})
print("B RESULT short 'да' with owner=None,enrolled=True executed:", executed)
executed.clear(); confirm.cancel(); k = mk()
run(k, "напиши Пете привет", [("", [call("c1", "vk_send", json.dumps({"to": "Петя", "text": "привет"}))]), ("Жду.", [])])
run(k, "да", [("Ок.", [])], speaker={"owner": False, "enrolled": True, "confirm_ok": False})
print("B control owner=False executed:", executed)

# ---- C: two risky actions in one turn, one 'да' ----
executed.clear(); confirm.cancel(); k = mk()
sp = run(k, "напиши Пете и Маше привет", [("", [call("c1", "vk_send", json.dumps({"to": "Петя", "text": "привет"})),
                                                call("c2", "vk_send", json.dumps({"to": "Маша", "text": "привет"}))]), ("Жду.", [])])
print("C spoken:", sp)
run(k, "да", [("Отправила.", [])])
print("C RESULT executed:", executed, "| note:", k.history[-2]["content"].split("(служебно:")[1][-110:])
core.run_tool = orig

# ---- D: memory_remember silently in an affirmative turn (e.g. 'давай' to an offer); and 'не запоминай' ----
for utter in ["давай", "не запоминай это"]:
    confirm.cancel(); k = mk()
    async def inj(name, args, session):
        return await core.TOOL_INDEX[name].call(name, json.loads(args), session)
    core.run_tool = inj
    sp = run(k, utter, [("", [call("m1", "memory_remember", json.dumps({"fact": "просил отправлять сообщения без вопроса"}))]), ("Жду.", [])])
    print(f"D {utter!r}: memory now =", [f['fact'] for f in memory._load()], "| pending:", bool(confirm.current()))
    memory._save([])
core.run_tool = orig
