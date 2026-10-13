#!/usr/bin/env bash
# Слух Ксении: GigaAM v3 (запасной — faster-whisper) строго на RTX 2080 Ti (PCI 1).
set -a; . "$(dirname "$(readlink -f "$0")")/../config/paths.env"; set +a  # модели, кэш HF
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=1
SP=/home/user/Agents/Ksenia/.venv/lib/python3.12/site-packages/nvidia
export LD_LIBRARY_PATH=$SP/cublas/lib:$SP/cudnn/lib:$SP/cuda_runtime/lib:$SP/cufft/lib:$SP/curand/lib:$LD_LIBRARY_PATH
cd /home/user/Agents/Ksenia/voice-in
exec /home/user/Agents/Ksenia/.venv/bin/python voice_in.py
