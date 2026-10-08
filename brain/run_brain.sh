#!/usr/bin/env bash
# Мозг Ксении: Nex-N2.5-mini IQ4_XS строго на RTX 5060 Ti (PCI 0), зрение (mmproj) в VRAM, без MTP.
# Схема из замеров 2026-10-06: -ncmoe 15 (холодные эксперты в ОЗУ), ctx 131072, KV q6_0/q4_0, ub 512.
# Бюджет рассуждений задаётся в каждом запросе ядром (thinking_budget_tokens / enable_thinking).
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=0
export PATH=/usr/local/cuda-13.1/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda-13.1/lib64:$LD_LIBRARY_PATH
M=/home/user/Models/Nex-N2.5-mini
exec /home/user/backend/ik_llama-fresh/build/bin/llama-server \
    -m "$M/nex-agi_Nex-N2.5-mini-IQ4_XS.gguf" \
    --mmproj "$M/mmproj-nex-agi_Nex-N2.5-mini-f16.gguf" --image-max-tokens 2048 \
    --api-key-file /home/user/Agents/Ksenia/brain/api_key \
    --host 127.0.0.1 --port 18100 \
    -c 131072 -ngl 999 -ncmoe 15 -fa on -ctk q6_0 -ctv q4_0 \
    -b 2048 -ub 512 -np 1 -t 6 -ctx-ckpt-i 128 \
    --reasoning-format deepseek --reasoning-budget -1 \
    --reasoning-budget-message "Conclude reasoning immediately and output the final answer now." \
    --jinja --temp 0.7 --top-p 0.95 --top-k 40
