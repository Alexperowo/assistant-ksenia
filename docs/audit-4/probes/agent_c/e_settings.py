import sys, asyncio
sys.path.insert(0, "/home/user/assistant-ksenia/core")
from tools import settings
calls = []
out = {"nmcli": "yes:Cafe\\:5G:70\n"}
async def fake_run(*argv, timeout=15):
    calls.append(argv)
    if argv[0] == "wpctl" and argv[1] == "get-volume": return 0, "Volume: 0.00 [MUTED]"
    if argv[0] == "nmcli": return 0, out["nmcli"]
    return 0, ""
settings._run = fake_run
for a in ({"action": "mute"}, {"action": "volume_set", "value": "0"}):
    r = asyncio.run(settings.call("setting", a, None)); print(a, "->", r, "| argv:", calls[-2] if a["action"] == "volume_set" else calls[-1])
try:
    print(asyncio.run(settings.call("setting", {"action": "wifi_status"}, None)))
except Exception as e:
    print("wifi_status with SSID 'Cafe:5G' ->", repr(e))
