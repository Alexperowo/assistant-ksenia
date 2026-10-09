#!/bin/bash
# Судья реплик живого режима: маленькая модель на RTX 2080 Ti (CUDA1), чтобы решать «перебил или поддакнул»,
# не отнимая основной мозг (он в это время рассказывает). Ответ — одна буква, контекст короткий.
# Выбор (2026-10-09, 48 фраз из живых тестов): NeoHorse-1-4B 46/48 за 0,11 с — ошибки безобидные;
# Qwen3.5-4B 46, Agents-A1-4B 45, Spark-X2.5-4B 44, Qwen3.5-2B 41; основной мозг 48, но 0,4 с и только пока свободен.
MODEL="${JUDGE_MODEL:-$HOME/Models/judge/NeoHorse-1-4B-Q4_K_M.gguf}"
exec "$HOME/backend/llama.cpp-mainline/build/bin/llama-server" \
  -m "$MODEL" --device CUDA1 -ngl 99 -c 4096 -np 1 --host 127.0.0.1 --port "${JUDGE_PORT:-18150}" \
  -fa on --cache-reuse 256 --jinja --reasoning off \
  --no-webui --log-disable
