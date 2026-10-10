#!/usr/bin/env bash
# Голос Ксении: Fish Audio S2 Pro (q8_0) через s2.cpp (ветка ksenia-perf) строго на RTX 2080 Ti (PCI 1).
# HTTP: POST /generate (multipart: text, voice=masha, params JSON). Голоса: voice-out/voices/*.s2voice
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=1
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
"$(dirname "$0")/../scripts/wait-gpu.sh" 1 || exit 1  # драйвер после загрузки системы ещё не готов
# S2_CODEC_PROF=1 — замеры кодека в журнал (только при отладке: s2 смотрит лишь на наличие переменной)
export S2_CODEC_TAIL=8   # тяжёлая часть кодека только по новым кадрам + 8 кадров разгона (проверено: 46,8 дБ к эталону)
exec /home/user/backend/s2.cpp/build/s2 --server -H 127.0.0.1 -P 18110 \
    -m /home/user/Models/Speech/fish-s2-pro/s2-pro-q8_0.gguf \
    -t /home/user/Models/Speech/fish-s2-pro/tokenizer.json \
    --cuda 0 -ngl 99 -threads 4 --codec-follow-backend \
    --voice-dir /home/user/Agents/Ksenia/voice-out/voices
