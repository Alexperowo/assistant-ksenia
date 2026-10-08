"""Конец реплики по смыслу и интонации: Smart Turn v3.2 (pipecat-ai, ONNX, CPU ~15 мс).

Раньше реплика заканчивалась после 700 мс тишины — Ксения перебивала Александра, когда он
задумывался посреди фразы (микрофон JBL ещё и глушит паузы до нуля). Теперь после короткой паузы
модель решает «договорил / нет»; если нет — ждём дольше. Русский в замерах авторов: 93,5 %.
Признаки — log-mel как у Whisper (WhisperFeatureExtractor, chunk_length=8), без transformers.
"""
import os

import numpy as np

MODEL = "/home/user/Models/Speech/turn/smart-turn-v3.2-cpu.onnx"
RATE = 16000
N_SAMPLES = 8 * RATE


def dither_zeros(pcm16: np.ndarray, min_run: int = 160, seed: int = 0) -> np.ndarray:
    """Микрофон JBL глушит паузы до цифрового нуля. Модель учили на живых записях с фоновым шумом, и абсолютная
    тишина для неё — нетипичный признак «договорил» (0,97–0,99 даже посреди фразы, живой тест 2026-10-08).
    Заполняем нулевые участки длиннее min_run отсчётов (10 мс) тихим шумом ~ на 46 дБ ниже речи."""
    x = np.asarray(pcm16, dtype=np.int16)
    zero = x == 0
    if zero.sum() < min_run:
        return x
    voiced = x[~zero].astype(np.float32)
    level = max(4.0, 0.005 * float(np.sqrt(np.mean(voiced ** 2))) if voiced.size else 4.0)
    out = x.copy()
    # границы серий нулей
    d = np.diff(np.concatenate(([0], zero.astype(np.int8), [0])))
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    rng = np.random.default_rng(seed)
    for a, b in zip(starts, ends):
        if b - a >= min_run:
            out[a:b] = np.clip(rng.normal(0, level, b - a), -32768, 32767).astype(np.int16)
    return out


class TurnDetector:
    def __init__(self, path=MODEL):
        import onnxruntime as ort
        import torch
        import torchaudio
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.intra_op_num_threads = 2
        self.sess = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
        self.torch = torch
        self.window = torch.hann_window(400)
        self.mel = torchaudio.functional.melscale_fbanks(201, 0.0, 8000.0, 80, RATE, norm="slaney",
                                                         mel_scale="slaney")  # [201, 80]
        self.complete(np.zeros(RATE, dtype=np.int16))  # прогрев

    def features(self, pcm16: np.ndarray) -> np.ndarray:
        x = pcm16.astype(np.float32) / 32768.0
        x = x[-N_SAMPLES:]
        x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)  # do_normalize=True — по реальной части, до добивки нулями
        x = np.pad(x, (0, N_SAMPLES - len(x)))
        t = self.torch.from_numpy(x)
        stft = self.torch.stft(t, 400, 160, window=self.window, return_complex=True)
        power = stft[..., :-1].abs() ** 2                      # [201, 800]
        mel = (self.mel.T @ power).clamp(min=1e-10).log10()
        mel = self.torch.maximum(mel, mel.max() - 8.0)
        return ((mel + 4.0) / 4.0).numpy()[None].astype(np.float32)  # [1, 80, 800]

    def complete(self, pcm16: np.ndarray) -> float:
        """Вероятность, что человек договорил (0…1). Нулевые паузы заполняет вызывающий (dither_zeros)."""
        return float(self.sess.run(None, {"input_features": self.features(pcm16)})[0][0][0])


def load(log=None):
    if not os.path.exists(MODEL):
        if log:
            log.warning("Smart Turn не найден (%s) — конец реплики по тишине", MODEL)
        return None
    try:
        return TurnDetector()
    except Exception as e:
        if log:
            log.warning("Smart Turn не загрузился: %r — конец реплики по тишине", e)
        return None
