import sys, asyncio, os, time
sys.path.insert(0, "/home/user/assistant-ksenia/core")
from tools import confirm, system
system.PM = "apt"
captured = []
async def fake_exec(argv, timeout):
    captured.append(argv); return 100, "Reading package lists...\nE: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 4242 (apt-get)"
real_exec = system._exec
system._exec = fake_exec
for p in ["pipewire-", "network-manager-", "sddm-", "plasma-workspace", "pipewire-bin", "libpipewire-0.3-0t64"]:
    for cmd in ("install", "remove"):
        r = asyncio.run(system.call("system", {"command": cmd, "param": p}, None))
        if r.get("prepared"):
            res = asyncio.run(confirm.take()["run"]())
            print(f"{cmd:7s} {p:22s} asked={r['speak_verbatim']!r:45s} argv tail={captured[-1][-3:]}")
        else:
            print(f"{cmd:7s} {p:22s} refused: {r.get('error')}")
print("\nresult dict of a failed change (what core turns into 'НЕ удалось: {error}'):")
print(res, "\n-> res.get('error') =", res.get("error"))
# search_package option-like param
r = asyncio.run(system.call("system", {"command": "search_package", "param": "--full"}, None))
print("\nsearch_package '--full' argv:", captured[-1])

# outer 60 s cap in core._resolve_confirmation vs per-command timeouts (scaled: 1 s cap, 'apt-get' = sleep 4)
system._exec = real_exec
procs = []
orig = asyncio.create_subprocess_exec
async def spy(*a, **k):
    p = await orig(*a, **k); procs.append(p); return p
system.asyncio.create_subprocess_exec = spy
system.CHANGE["install"] = (*system.CHANGE["install"][:2], lambda p: ["sleep", "4"], 900)
async def core_like():
    r = await system.call("system", {"command": "install", "param": "vlc"}, None)
    item = confirm.take()
    try:
        res = await asyncio.wait_for(item["run"](), timeout=1)   # core.py uses timeout=60
    except Exception as e:
        res = {"ok": False, "error": f"сбой: {e!r}"[:200]}
    print("\ncore would say: НЕ удалось:", res.get("error"))
    pid = procs[-1].pid
    alive = os.path.exists(f"/proc/{pid}") and open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    print(f"child 'apt-get' (pid {pid}) still running after the cap: {alive}")
asyncio.run(core_like())
