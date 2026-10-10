import sys, types; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest, numpy as np
sys.modules.setdefault("torch", types.ModuleType("torch")); sys.modules.setdefault("torchaudio", types.ModuleType("torchaudio"))
import voiceprint
vp = voiceprint.Voiceprint.__new__(voiceprint.Voiceprint)
vp.owner_th, vp.confirm_th, vp.pending, vp.last = 0.5, 0.55, [], None
vp.centroid = np.ones(256, np.float32) / 16   # enrolled
da = (0.1 * np.sin(np.arange(int(16000 * 0.55)) / 3) * 32767).astype(np.int16)  # live-mode "да": 0.3 s pre-roll + word + 0.2 s tail
res = vp.check(da)
print("vp.check(0.55 s 'да') ->", res)
# core/core.py respond(): guest / weak_voice exactly as written there
speaker = res
guest = speaker.get("owner") is False
weak_voice = speaker.get("enrolled") and speaker.get("owner") and not speaker.get("confirm_ok")
print("core: guest =", guest, " weak_voice =", weak_voice, "-> confirmation 'да' goes to _resolve_confirmation (accepted)")
