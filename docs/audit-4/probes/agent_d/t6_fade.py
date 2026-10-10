import sys, asyncio, time; sys.path.insert(0, "/home/user/assistant-ksenia/core"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest, numpy as np, logging
import core
logging.disable(logging.CRITICAL)
core.CONFIG.update({"record_replies": False})
class Stdin:
    def __init__(s): s.data = bytearray(); s.closed = False
    def write(s, b): s.data.extend(b)
    async def drain(s): await asyncio.sleep(10)   # pipe full: nothing more accepted -> chunks stay in Speaker._q
    def close(s): s.closed = True
class Proc:
    def __init__(s): s.stdin = Stdin(); s.returncode = None
    def kill(s): s.returncode = -9
    async def wait(s): await asyncio.sleep(0.01); s.returncode = s.returncode or 0; return s.returncode
async def scenario(gain, first_len):
    sp = core.Speaker(None); sp.set_volume(int(gain*100)); proc = sp.player = Proc()
    tone = (np.ones(44100, dtype=np.int16) * 10000).tobytes()
    # what speak() does per chunk: gain (only when gain != 1 or carry), then _send
    for c in (tone[:first_len], tone[first_len:first_len+8192], tone[first_len+8192:first_len+16384]):
        if sp.gain != 1.0 or sp._carry:
            c = sp._apply_gain(c)
        sp._send(c)
    await asyncio.sleep(0.01)  # play loop writes the first chunk, then blocks in drain; rest stays queued
    written = len(sp.player.stdin.data)
    await sp.cancel()
    raw = bytes(proc.stdin.data)
    # what the sound card plays: the byte stream read as int16 from offset 0
    x = np.frombuffer(raw[:len(raw)//2*2], dtype=np.int16)
    w = written // 2
    f = np.abs(x[w:]).astype(int); print("  fade len", len(f), "samples above input level:", int((f > 10000*gain+1).sum()), "rms", int(np.sqrt((f.astype(float)**2).mean())))
    print(f"gain={gain} first chunk {first_len} B: level before stop={x[w-2]}, first fade samples={x[w:w+4].tolist()}, "
          f"max |x| in fade={np.abs(x[w:]).max()} (expected <= {int(10000*gain)})")
asyncio.run(scenario(0.75, 8192))   # night: fade starts at gain^2
asyncio.run(scenario(1.0, 8191))    # day, odd-sized HTTP chunk: fade decodes misaligned bytes
