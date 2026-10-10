import sys; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest
import numpy as np, voice_in
RATE=16000
def frames(*parts):
    x = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype(np.int16)
    return [x[i:i + 320] for i in range(0, len(x) - 319, 320)]
def speech(s, amp=0.1): return amp*np.sin(2*np.pi*220*np.arange(int(RATE*s))/RATE)
def zeros(s): return np.zeros(int(RATE*s))
def noise(s, rms, seed=1): return np.random.default_rng(seed).normal(0, rms, int(RATE*s))

def end_time(text, ctx):
    seg = voice_in.LiveSegmenter(dict(voice_in.CONFIG)); seg.ctx.update(ctx)
    for k, f in enumerate(frames(zeros(0.5), speech(0.6), zeros(5))):
        ev = seg.push(f)
        if ev == "check_fast": ev = seg.decide(1.0, text, fast=True)
        elif ev == "check": ev = seg.decide(0.99, text)
        if ev == "end": return round(k*0.02 - 1.1, 2)
for t, c in [("Что это?", {}), ("Давай.", {"asked": True}), ("Который час?", {})]:
    print("live: silence after speech before end for", repr(t), c, "=", end_time(t, c), "s")

# (b) continuous background after speech start: quiet room (JBL zeros) -> he speaks -> TV/vacuum/kettle at rms 0.02 starts
cfg = dict(voice_in.CONFIG)
seg = voice_in.LiveSegmenter(cfg)
evs = {}
fr = frames(zeros(1.0), speech(1.0), noise(300, 0.02))
for k, f in enumerate(fr):
    ev = seg.push(f)
    if ev in ("check", "check_fast"): ev = seg.decide(0.99, "Включи музыку.", fast=(ev=="check_fast"))
    if ev: evs[ev] = evs.get(ev, 0) + 1
print("live: 1 s speech then 300 s background rms 0.02 (max_s=%s): events=%s, buffered=%.0f s (%.1f MB)" %
      (cfg.get("max_s"), evs, len(seg.frames)*0.02, len(seg.frames)*640/1e6), "noise est=", round(seg.noise,4))
# (c) same with step mode semantics: threshold at speech start
print("end-of-speech threshold while speaking:", max(seg.noise*2, cfg["min_speech_rms"]*0.7))
