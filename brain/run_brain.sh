#!/usr/bin/env bash
# ОСНОВНОЙ МОЗГ (с 2026-10-08): Ternary Bonsai 2 27B (Qwen3.8-27B, тернарные веса PQ2_0) + MTP-голова из Qwen3.8 + зрение, строго на RTX 5060 Ti.
# Движок: форк PrismML llama.cpp (prism-b10754-2459f68, ~/backend/llama.cpp-prism). Тот же порт и ключ, что у Nex.
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=0
export PATH=/usr/local/cuda/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
M=/home/user/Models/Bonsai-2-27B
exec /home/user/backend/llama.cpp-prism/src/build/bin/llama-server \
    -m "$M/Bonsai-2-27B-PQ2_0-MTP.gguf" \
    --mmproj "$M/Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf" --image-max-tokens 2048 \
    --api-key-file /home/user/Agents/Ksenia/brain/api_key \
    --host 127.0.0.1 --port 18100 \
    -c 131072 -np 2 -ngl 99 -fa on -ctk q8_0 -ctv q8_0 -b 2048 -ub 512 -t 6 \
    --spec-type draft-mtp --spec-draft-n-max 2 \
    --jinja --reasoning-format deepseek \
    --reasoning-budget-message "Conclude reasoning immediately and output the final answer now." \
    --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0.0 --presence-penalty 1.5
