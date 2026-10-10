import sys, asyncio; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in")
import headset
T = [0.0]
async def run(*argv, input_text=None, timeout=20):
    # bluetoothd did not come back after `systemctl restart bluetooth`: bluetoothctl waits for it until the timeout
    if argv[0] == "bluetoothctl":
        T[0] += timeout; return -1, "timeout"
    T[0] += 0.05; return 0, ""
real_sleep = asyncio.sleep
async def vsleep(s): T[0] += s; await real_sleep(0)
headset.run = run; headset.asyncio.sleep = vsleep
async def main():
    ok = await headset.restart_bluetooth("88:92:CC:00:11:22")
    print(f"restart_bluetooth -> {ok}, virtual time spent: {T[0]/60:.1f} min (nominal wait 15 s)")
asyncio.run(main())
