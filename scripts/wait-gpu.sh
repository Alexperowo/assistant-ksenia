#!/usr/bin/env bash
# Ждать, пока видеокарта (номер по шине PCI: 0 — RTX 5060 Ti, 1 — RTX 2080 Ti) готова к CUDA.
# Сразу после загрузки системы драйвер ещё поднимается: llama.cpp тогда пишет «failed to initialize CUDA»
# и МОЛЧА работает на процессоре (2026-10-10: мозг отвечал по 3 минуты). Лучше подождать, а не стартовать так.
# Без ответа за 120 с — выход с ошибкой, systemd перезапустит службу.
GPU="${1:?номер видеокарты}"
PROBE="$HOME/backend/llama.cpp-mainline/build/bin/llama-server"
for i in $(seq 1 60); do
    if CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES="$GPU" LD_LIBRARY_PATH=/usr/local/cuda/lib64 \
            timeout 20 "$PROBE" --list-devices 2>/dev/null | grep -q "CUDA0:"; then
        [ "$i" -gt 1 ] && echo "wait-gpu: видеокарта $GPU готова через $((i * 2)) с"
        exit 0
    fi
    sleep 2
done
echo "wait-gpu: видеокарта $GPU не готова за 120 с" >&2
exit 1
