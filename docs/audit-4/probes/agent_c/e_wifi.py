"""Virtual-clock timeline of settings._wifi_connect against the core's 60 s cap for confirmed actions."""
import sys, asyncio, types
sys.path.insert(0, "/home/user/assistant-ksenia/core")
from tools import settings
clock = {"t": 0.0}
def scenario(name, connect, check):
    clock["t"] = 0.0
    events = []
    async def fake_run(*argv, timeout=15):
        if argv[:3] == ("nmcli", "-t", "-f"):
            cost, out = 0.2, "yes:HomeNet:70\n"
        elif argv[:4] == ("nmcli", "dev", "wifi", "connect"):
            cost, rc, out = connect
            cost = min(cost, timeout)
            clock["t"] += cost; events.append((round(clock["t"]), "connect rc=%s" % rc)); return rc, out
        elif argv[:3] == ("nmcli", "con", "up"):
            cost, out = 10, "Connection successfully activated"
            clock["t"] += cost; events.append((round(clock["t"]), "ROLLBACK con up " + argv[-1])); return 0, out
        elif argv[:3] == ("nmcli", "networking", "connectivity"):
            cost, out = check, None
            clock["t"] += cost[0]; return 0, cost[1]
        clock["t"] += cost; return 0, out
    async def fake_sleep(s): clock["t"] += s
    settings._run = fake_run
    settings.asyncio = types.SimpleNamespace(sleep=fake_sleep)
    res = asyncio.run(settings._wifi_connect("CafeFree", "secret123"))
    print(f"--- {name}: finished at t={clock['t']:.0f}s (core cap 60s) -> {res}")
    for t, e in events: print(f"    t={t:>3}s {e}" + ("   <-- after the 60 s cap: core already said 'сбой: TimeoutError()'" if t > 60 else ""))
scenario("A captive portal / no internet", (8, 0, "Device successfully activated"), (2, "portal"))
scenario("B wrong password (nmcli waits, killed at 45 s)", (90, -1, "timeout"), (2, "none"))
scenario("C host has connectivity checking disabled", (8, 0, "Device successfully activated"), (0.2, "unknown"))
scenario("D associated but no route/DNS (each connectivity check ~6 s)", (8, 0, "Device successfully activated"), (6, "none"))
