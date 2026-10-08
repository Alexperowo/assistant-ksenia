#!/usr/bin/env python3
"""Прогнать свои записи (logs/listens/*.wav — их сохраняет слух) через нарезку живого режима и показать,
где реплика закончилась бы: со старой логикой (только Smart Turn + «висящее» слово), с новой (текст важнее,
быстрая проверка 0,4 с) и с заполнением нулевых пауз шумом и без. Ничего не меняет, только печатает.

Запуск (на компьютере Ксении, где есть модели):
  cd ~/Agents/Ksenia/voice-in && ../.venv/bin/python ../tools-scripts/turn_replay.py [файлы...]
"""
import glob
import os
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "voice-in"))
import turn  # noqa: E402
import voice_in  # noqa: E402


def replay(ear, pcm, dither, fast):
    cfg = dict(voice_in.CONFIG, turn_dither=dither, turn_fast_ms=400 if fast else 0, partial_ms=0)
    voice_in.CONFIG.update(cfg)
    seg = voice_in.LiveSegmenter(cfg)
    ends = []
    for k in range(0, len(pcm) - 319, 320):
        ev = seg.push(pcm[k:k + 320])
        if ev == "check_fast":
            ev = seg.decide(1.0, ear.transcribe(seg.pcm()), fast=True)
        elif ev == "check":
            ev = seg.decide(*ear.turn_check(seg.pcm()))
        if ev == "end":
            ends.append((round(k / 16000, 2), seg.texts[-1] if seg.texts else "", seg.probs))
            seg.utterance()
    return ends


def main():
    files = sys.argv[1:] or sorted(glob.glob(os.path.join(HERE, "..", "logs", "listens", "*.wav")))[-10:]
    ear = voice_in.Ear()
    for f in files:
        pcm, sr = sf.read(f, dtype="int16")
        if sr != 16000:
            print(f"{f}: не 16 кГц, пропускаю")
            continue
        zeros = float(np.mean(pcm == 0))
        print(f"\n{os.path.basename(f)}: {len(pcm) / 16000:.1f} с, нулей {zeros:.0%}")
        for dither in (False, True):
            for fast in (False, True):
                ends = replay(ear, pcm, dither, fast)
                print(f"  шум в паузах={'да ' if dither else 'нет'} быстрая проверка={'да ' if fast else 'нет'}: "
                      + "; ".join(f"{t} с «{txt}» p={p}" for t, txt, p in ends))


if __name__ == "__main__":
    main()
