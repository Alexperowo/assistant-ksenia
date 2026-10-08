#!/usr/bin/env bash
# Слух Ксении: faster-whisper large-v3-turbo строго на RTX 2080 Ti (PCI 1).
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=1
SP=/home/user/Agents/Ksenia/.venv/lib/python3.12/site-packages/nvidia
export LD_LIBRARY_PATH=$SP/cublas/lib:$SP/cudnn/lib:$LD_LIBRARY_PATH
cd /home/user/Agents/Ksenia/voice-in
exec /home/user/Agents/Ksenia/.venv/bin/python voice_in.py
