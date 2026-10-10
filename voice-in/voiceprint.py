"""Отпечаток голоса Александра: WeSpeaker ResNet34-LM (ONNX, CPU, ~50 мс на фразу).

Хранится только усреднённый вектор (192… числа), не запись голоса: data/voiceprint.json.
Пороги подобраны по замерам на корпусе Dialogs (2026-10-08):
  owner_threshold 0.50 — «это он» (своих отвергается ~3%, чужих принимается ~3%);
  confirm_threshold 0.55 — для «да» на рискованное действие (чужих принимается <1%).
"""
import json
import os
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL = "/home/user/Models/Speech/speaker/wespeaker-resnet34.onnx"
FILE = os.path.normpath(os.path.join(ROOT, "..", "data", "voiceprint.json"))


class Voiceprint:
    def __init__(self, owner_threshold=0.50, confirm_threshold=0.55):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
        self.owner_th, self.confirm_th = owner_threshold, confirm_threshold
        self.centroid = self._load()
        self.pending = []  # векторы фраз при записи образца
        self.last = None   # вектор последней реплики
        self.embed((np.random.default_rng(0).standard_normal(16000) * 300).astype(np.int16))  # прогрев torch/ONNX

    def _load(self):
        try:
            with open(FILE, encoding="utf-8") as f:
                c = np.array(json.load(f)["centroid"], dtype=np.float32)
            return c / np.linalg.norm(c)
        except FileNotFoundError:
            return None
        except Exception:
            os.replace(FILE, FILE + time.strftime(".bad-%Y%m%d-%H%M%S"))
            return None

    def embed(self, pcm16: np.ndarray):
        import torch
        import torchaudio
        if len(pcm16) < 16000 * 0.6:  # слишком коротко для надёжного отпечатка
            return None
        if len(pcm16) > 16000 * 8:  # для узнавания хватает 8 с: середина фразы (на CPU ~50 мс вместо ~1,5 с)
            mid = len(pcm16) // 2
            pcm16 = pcm16[mid - 16000 * 4: mid + 16000 * 4]
        w = torch.from_numpy(pcm16.astype(np.float32)).unsqueeze(0)  # шкала int16 — как у kaldi
        f = torchaudio.compliance.kaldi.fbank(w, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0,
                                              sample_frequency=16000)
        f = (f - f.mean(0, keepdim=True)).numpy()[None].astype(np.float32)
        e = self.sess.run(None, {"input_features": f})[0][0]
        return e / np.linalg.norm(e)

    def check(self, pcm16: np.ndarray) -> dict:
        """Сверить реплику с отпечатком. owner: True/False, или None — образца ещё нет / фраза слишком коротка."""
        e = self.embed(pcm16)
        self.last = e
        if e is None or self.centroid is None:
            return {"owner": None, "enrolled": self.centroid is not None}
        s = float(e @ self.centroid)
        return {"owner": s >= self.owner_th, "confirm_ok": s >= self.confirm_th, "score": round(s, 3), "enrolled": True}

    def add_last(self) -> dict:
        if self.last is None:
            return {"ok": False, "error": "последняя фраза слишком короткая или её нет"}
        self.pending.append(self.last)
        return {"ok": True, "collected": len(self.pending)}

    def save(self, min_phrases=4) -> dict:
        if len(self.pending) < min_phrases:
            return {"ok": False, "error": f"мало фраз: {len(self.pending)} из {min_phrases}"}
        c = np.mean(self.pending, 0)
        c = c / np.linalg.norm(c)
        # фраза, совсем не похожая на остальные (чужой голос рядом, шум) — не в образец
        keep = [v for v in self.pending if v @ c >= 0.45]
        if len(keep) < min_phrases:
            return {"ok": False, "error": f"фразы слишком разные ({len(keep)} похожих из {len(self.pending)}) — "
                                          f"запишем ещё раз в тишине"}
        if len(keep) != len(self.pending):
            c = np.mean(keep, 0)
            c = c / np.linalg.norm(c)
        self.pending = keep
        spread = float(np.mean([v @ c for v in self.pending]))  # насколько фразы похожи между собой
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"centroid": c.tolist(), "phrases": len(self.pending), "self_similarity": round(spread, 3),
                       "created": time.strftime("%Y-%m-%d %H:%M"), "model": os.path.basename(MODEL)}, f)
        os.replace(tmp, FILE)
        self.centroid, self.pending = c, []
        return {"ok": True, "self_similarity": round(spread, 3)}

    def clear(self) -> dict:
        self.pending = []
        self.centroid = None
        if os.path.exists(FILE):
            os.replace(FILE, FILE + time.strftime(".old-%Y%m%d-%H%M%S"))
        return {"ok": True}
