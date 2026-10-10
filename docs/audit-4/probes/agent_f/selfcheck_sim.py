import sys, os, asyncio
sys.dont_write_bytecode = True
S = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, S + "/snap/core")
from tools import selfcheck
async def fake_run(*argv):
    a = " ".join(argv)
    if "is-active" in a: return 0, "active"           # hung processes are still "active"
    if "pactl" in a: return 0, "51\tbluez_output.AA_BB.1\tPipeWire"
    if "18120" in a: return -1, "TimeoutError()"        # voice-in hung after resume
    if "nvidia-smi" in a: return -1, "TimeoutError()"   # driver hung: nvidia-smi never returns
    if a.startswith("free"): return 0, "Mem: 46000 10000 30000 0 6000 35000"
    return 0, ""
async def fake_http(session, url, headers=None): return 200
selfcheck._run = fake_run; selfcheck._http = fake_http
selfcheck.CFG["brain_key_file"] = "/dev/null"
os.path.exists = (lambda f, _e=os.path.exists: True if "yandex_music_token" in f else _e(f))
r = asyncio.run(selfcheck.call("self_check", {}, None))
print("problems:", r["problems"]); print("fine:", r["fine"])
