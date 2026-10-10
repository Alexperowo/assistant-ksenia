import sys, types, numpy as np
sys.path.insert(0, "voice-in")
for m in ("torch", "torchaudio", "onnxruntime"):
    sys.modules.setdefault(m, types.ModuleType(m))
import voiceprint
cls = [c for c in vars(voiceprint).values() if isinstance(c, type) and hasattr(c, "check")][0]
vp = cls.__new__(cls); vp.centroid = np.ones(4) / 2; vp.owner_th, vp.confirm_th = 0.5, 0.6; vp.last = None
print("0.45 s 'да' from anyone ->", vp.check(np.zeros(int(16000 * 0.45), dtype=np.int16)))
