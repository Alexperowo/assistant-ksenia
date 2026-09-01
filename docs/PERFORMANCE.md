# Performance / Acceptance Harness

## Назначение

Этот слой отвечает на практический вопрос: «Где именно Ксения потеряла время в конкретной реплике?» Он расширяет существующий безопасный `runtime/logs/diagnostics.jsonl`, не создаёт отдельный telemetry-сервис, не пишет текст разговора и не меняет рабочие настройки.

Одна пользовательская реплика получает непрозрачный `trace_id`. Дополнительно используются:

- `turn_id` — конкретный разговорный ход;
- `task_id` — запись долговечной задачи;
- `request_id` — отдельный STT, LLM или TTS-запрос;
- `run_id` — необязательный идентификатор серии benchmark-прогонов.

Контекст проходит через синхронный agent/orchestrator/tool-контур автоматически. Долгоживущие STT и TTS reader threads сохраняют снимок идентификаторов в момент постановки запроса и восстанавливают его только для ответа с тем же `request_id`. Фоновые heartbeat threads получают отдельный неизменяемый снимок контекста; он не протекает в следующую задачу.

## Milestones

События с `component=performance` являются точками общей временной шкалы:

| Event | Текущее значение |
|---|---|
| `voice_start` | STT worker впервые подтвердил человеческую речь |
| `voice_end` | завершён захват основной реплики |
| `turn_detected` | endpoint detector выбрал конец реплики |
| `stt_partial_first` | получен первый непустой partial без сохранения текста |
| `stt_final` | получен качественный финальный transcript; сам текст редактируется |
| `llm_request_start` | OpenAI-compatible запрос передан HTTP transport |
| `llm_first_token` | получен первый content/reasoning/tool-call fragment |
| `llm_generation_end` | чтение ответа завершено, закрыто или оборвано |
| `tts_first_chunk_ready` | Silero закончил синтез фразы и подготовил WAV |
| `audio_first_played` | audio backend начал вызов воспроизведения |
| `audio_finished` | backend подтвердил окончание или отказ фразы |
| `interrupt_detected` | voice/headset stop принят до отмены задачи и audio stop |
| `audio_actually_stopped` | TTS worker вернул cancelled completion для остановленного запроса |
| `llm_actually_cancelled` | cooperative checkpoint реально вышел из HTTP generation |

`audio_first_played` пока является наблюдением на границе software playback backend, а не акустическим датчиком динамика. Реальную задержку «динамик → микрофон» сможет измерять только будущая AEC/device calibration. Отчёт не выдаёт эту границу за уже решённую.

Одна trace может содержать приглашение «Слушаю», голосовые статусы, несколько LLM tool turns и несколько TTS-фраз. Для TTFA отчёт выбирает первую TTS/audio-точку не раньше `llm_first_token` (или `stt_final` для безмодельного маршрута), поэтому приглашение и ранние статусы не подменяют начало ответа. Для `llm_generation_end` и `audio_finished` берётся последняя подходящая точка хода.

## Сводка

Обычный отчёт по активному и ротированным журналам:

```powershell
python scripts\performance-report.py
```

Машинный JSON:

```powershell
python scripts\performance-report.py --json
```

Одна trace:

```powershell
python scripts\performance-report.py --trace-id <trace_id>
```

Для узкого сценария список обязательных milestones задаётся повторяемым параметром:

```powershell
python scripts\performance-report.py `
  --required-milestone interrupt_detected `
  --required-milestone audio_actually_stopped `
  --required-milestone llm_actually_cancelled
```

По умолчанию проверяется полный voice-response pipeline. Текстовая, fast-intent или отменённая trace закономерно может быть отмечена неполной; это не следует скрывать искусственными timestamps. Для каждого числового ряда вычисляются `count`, `min`, `average`, `p50`, `p95`, `max`. Percentiles используют линейную интерполяцию отсортированного ряда.

Кроме waterfall рассчитываются безопасные длительности существующих компонентов: model start/stop, agent/tool selection, tool execution, RAG search, web research, STT, TTS и LLM. `completion_completed` отдельно пишет prompt/completion/total tokens и эффективную скорость всего HTTP completion. Все chat-запросы явно закрепляют `cache_prompt=true`; completion и простой streaming transport сохраняют числовые `cached_prompt_tokens` и `prompt_cache_hit_ratio`, но не содержимое prefix. Streaming-запрос просит `stream_options.include_usage`; если конкретный совместимый backend не возвращает usage, поля остаются `null` и не попадают в статистику. Эта скорость полезна как пользовательская end-to-end метрика, но не заменяет нативные timings `llama.cpp` в модельном benchmark. Фактический context/cache baseline двух рабочих моделей находится в `RUNTIME-OPTIMIZATION-BASELINE.md`.

### Подтверждённый холодный tool-prefix bottleneck — 1 сентября 2026

Физическая голосовая trace с Tour One M3 показала: открытие микрофона заняло около 0,9 с, финальный faster-whisper — 0,7 с, а Laguna загрузилась за 9,1 с. После этого первый содержательный токен полного agent prompt не появился более чем за 93 с. Контрольный `runtime_context_benchmark.py` разделил влияние KV и prefix:

| Context | GPU после загрузки | Private memory | Cold TTFT, 2215 prompt tokens | Next-turn TTFT, 2221 cached tokens |
|---:|---:|---:|---:|---:|
| 16384 | 13812 МБ | 13,43 ГБ | 64336 мс | 1657 мс |
| 98304 | 16712 МБ | 16,70 ГБ | 63851 мс | 1847 мс |

Следовательно, уменьшение KV экономит около 2,9 ГБ VRAM и 3,3 ГБ private memory, но не лечит холодную оценку полного набора схем. Измеримый рычаг здесь — стабильный prefix и выбор `CHAT` до начала короткой разговорной задачи; набор инструментов внутри задачи по-прежнему не уменьшается.

Отменяемый HTTP transport наблюдает жизненный цикл вспомогательного reader thread. На каждой остановке доступны `active_reader_threads`, накопительный `cancelled_streams`, `reader_shutdown_latency_ms` и текущее число `stuck_reader_threads`. При отмене transport сначала делает `shutdown()` loopback-сокета, ждёт reader не более 100 мс и, если тот ещё занят, переносит `close()` в служебный daemon-thread. Неостановившийся reader отмечается warning и остаётся учтённым до фактического выхода; это сигнал для решения о замене transport, а не повод блокировать разговор или заранее переписывать весь HTTP-клиент.

## Приватность и совместимость

- Схема JSONL остаётся append-only и обратно совместимой.
- Старые строки без `monotonic_ns` читаются по UTC `time`; строки без `trace_id` входят в общие component metrics, но не притворяются трассами.
- Повреждённая строка учитывается счётчиком и не останавливает остальные данные.
- `trace_id`, `turn_id`, `task_id`, `request_id` генерируются кодом и не содержат пользовательский текст.
- Prompt, transcript, answer, query, command, URL parameters и credentials продолжают проходить прежнюю redaction policy.
- Журнал по-прежнему ротируется; отдельной БД, фонового отправителя и сетевой телеметрии нет.

## Acceptance-порядок

1. Зафиксировать сценарий и состояние устройств/модели.
2. Выполнить 20–30 повторов после одного холодного прогона.
3. Сохранить `trace_id` неудачного хода.
4. Сравнивать p50 и p95, а не один лучший результат.
5. После изменения повторить тот же corpus и условия.
6. Оставлять изменение только при измеримом выигрыше и зелёных regression tests.

Физические Live-сценарии с JBL, AEC и перебиванием остаются пользовательской приёмкой. Harness делает их измеримыми, но не заменяет реальный микрофон и динамик.
