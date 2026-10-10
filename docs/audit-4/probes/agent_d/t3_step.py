import sys, asyncio; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest
import numpy as np, voice_in
RATE=16000
def speech(s, amp=0.1): return amp*np.sin(2*np.pi*220*np.arange(int(RATE*s))/RATE)
def zeros(s): return np.zeros(int(RATE*s))
def noise(s, rms, seed=1): return np.random.default_rng(seed).normal(0, rms, int(RATE*s))
def pcm(*p): return (np.clip(np.concatenate(p), -1, 1)*32767).astype(np.int16).tobytes()
class Clock:
    t = 1000.0
    def time(self): return self.t
    def strftime(self, *a): return "x"
class FakeParec:
    def __init__(s, data, clock): s.data, s.pos, s.clock, s.returncode = data, 0, clock, None; s.stdout = s
    async def readexactly(s, n):
        if s.pos + n > len(s.data): raise asyncio.IncompleteReadError(b"", n)
        c = s.data[s.pos:s.pos+n]; s.pos += n; s.clock.t += n/2/RATE; return c
    def kill(s): s.returncode = -9
    async def wait(s): return s.returncode
class FakeTurn:
    def complete(self, pcm): return 0.99
def run(data, cfg=None, text=None):
    clock = Clock(); voice_in.time = clock
    voice_in.Ear._save_debug = staticmethod(lambda f: None)
    voice_in.CONFIG = {**voice_in.CONFIG, **(cfg or {})}
    p = FakeParec(data, clock)
    async def fake_exec(*a, **k): return p
    asyncio.create_subprocess_exec = fake_exec
    ear = voice_in.Ear.__new__(voice_in.Ear); ear.turn = FakeTurn()
    if text: ear.model = object(); ear.transcribe = lambda pcm: text
    else: ear.model = None
    return asyncio.run(ear.record_utterance("src", None))
# (a) speech then TV-level background: utterance only ends at max_s (config 120 s), core /listen timeout is 90 s
a, info = run(pcm(zeros(1.0), speech(1.5), noise(130, 0.02)))
print("step: speech + background 0.02 ->", info)
# (b) he waits 7 s after the beep, then says 3 s: returned audio includes the 7 s of leading silence
a, info = run(pcm(zeros(7.0), speech(3.0), zeros(2)))
print("step: 7 s pause then 3 s speech ->", info, "len(pcm) s =", len(a)/RATE)
# voiceprint embed crop (voiceprint.py:45-47) of that pcm
n = len(a); mid = n//2; lo, hi = mid-16000*4, mid+16000*4
sp0 = int(7.0*RATE)
print("voiceprint sees [%.2f..%.2f] s; speech in window: %.2f s of %.2f s speech (%.0f%% of window is silence)" %
      (lo/RATE, hi/RATE, (min(hi, n)-max(lo, sp0))/RATE, 3.0, 100*(sp0-lo)/(hi-lo)))
# (c) step-mode "Что?" -> waits 3 s silence (hanging rule) instead of 0.8
a, info = run(pcm(zeros(1.0), speech(0.4), zeros(5)), text="Что?")
print("step: 'Что?' ->", info)
a, info = run(pcm(zeros(1.0), speech(0.4), zeros(5)), text="Который час?")
print("step: 'Который час?' ->", info)
