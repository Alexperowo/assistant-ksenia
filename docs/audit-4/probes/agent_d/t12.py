exec(open("t2_segmenter.py").read().split("def end_time")[0])
# live-mode debug WAV = utterance with tail trimmed to 200 ms (what _save_debug([pcm]) stores); replay as turn_replay.py does
seg = voice_in.LiveSegmenter(dict(voice_in.CONFIG))
for f in frames(zeros(0.5), speech(1.0), zeros(2.0)):
    ev = seg.push(f)
    if ev in ("check", "check_fast"): ev = seg.decide(0.99, "Включи музыку.", fast=(ev == "check_fast"))
    if ev == "end": saved, _ = seg.utterance(); break
ends = []
cfg = dict(voice_in.CONFIG, partial_ms=0); seg = voice_in.LiveSegmenter(cfg)
for k in range(0, len(saved) - 319, 320):
    ev = seg.push(saved[k:k+320])
    if ev in ("check", "check_fast"): ev = seg.decide(0.99, "Включи музыку.", fast=(ev == "check_fast"))
    if ev == "end": ends.append(k / 16000)
print(f"saved live utterance {len(saved)/16000:.2f}s -> replay ends found: {ends}")
import sys; sys.path.insert(0, "/home/user/assistant-ksenia/core"); import core, logging; logging.disable(logging.CRITICAL)
sp = core.Speaker(None); sp.set_volume(150)
x = np.array([20000, -30000, 1000], dtype=np.int16).tobytes()
print("gain 1.5:", np.frombuffer(sp._apply_gain(x), dtype=np.int16).tolist(), "(expected clipped [32767, -32768, 1500])")
