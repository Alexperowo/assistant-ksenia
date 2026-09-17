OBJECTIVE:      Надёжность голосового ассистента Ксения (C0–C8), приёмка этапов и перевод в повседневное использование
PHASE:          C5/C6 завершены программно / 9 этапов (C0–C8)
STATE:          Официальный движок llama.cpp обновлён до b10991 (commit 930e2fa59, CUDA 12.4 x64). Устранена деградация FlashAttention при квантованных KV на Windows Clang (убран --flash-attn on, KV V установлен в q4_0), что обеспечило 64x ускорение обработки промпта (52.8 t/s против 0.25 t/s, 322 мс против 30.8 с). Все 531/531 тестов пройдены успешно (0 ошибок, 0 сбоев, 0 предупреждений) в 4 прогонах (2124 выполнения). Полный проверочный скрипт scripts/check.ps1 (в т.ч. с KSENIA_TEST_RAG=1) пройден с кодом 0 (Doctor green, LAN 200, RAG 5/5 live green, TTS->STT 100% recall). Все компоненты и lock-файлы (59 Python-пакетов, 2 llama-бэкенда) 100% совпадают (all_components_match: true). Все локальные порты (18080–18083, 8080) свободны.
DONE:
  - C0: Первичная инвентаризация и исходный воспроизводимый результат (commit 7cbcc74).
  - C1: Отказоустойчивость сессии и очистка TaskCancelled при 4 типах отказов (commit 9ea72c1).
  - C2: Разбор Thinking/Fast, авто-бюджеты исследований, изоляция пользовательских настроек (commits 70822f1, 37e83d2, 0e4356d).
  - C3: Непрерывный живой голосовой диалог, таймаут тишины 8 с, голосовые команды («Замолчи», «Отбой», «Выход») (commit f5cfabb).
  - C4: Фоновый веб-поиск на Agents-A1 параллельно разговору с UI-Mate, BackgroundResearchManager, изоляция сессии, речевой диспетчер (commit 97261ae).
  - C8 (RAG): Загружена закреплённая модель Qwen3-Embedding-0.6B-Q8_0.gguf (639 150 592 байт, SHA-256 06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439), выполнен живой запуск эмбеддера на CPU (порт 18081), пройдена инкрементальная индексация workspace и 5/5 поисковых сценариев с FTS5 + векторным поиском + RRF и отсечением no_match (commit 00d00c4).
  - C1+ HTTP Audit: Защита HTTP-транспорта stream_chat и count_chat_tokens через _open_completion_response (loopback enforce, bypass proxy, cancellation check при ожидании headers, exc.close() при HTTPError, 6 новых live-socket тестов, commit 202ce5c).
  - C6: Performance-метрики и milestones для фонового поиска (queued->started, started->completed, completed->delivered), сквозной trace_id, устранение race condition в test_cli_dialogue_recovery.py через детерминированный join рабочего потока (commit 2f1f1ee).
  - C5 (Обновление компонентов и движка): Обновлён официальный llama.cpp до релиза b10991 (commit 930e2fa59, CUDA 12.4 x64, SHA-256 DE86232A73A0FD5B96454309DA2993F2DC6BF5E16ECC506192F551D49D833CDF). Обновление проверено изолированно через scripts/test-engine-maintenance.ps1 и применено через scripts/update.ps1 с автобэкапом b10621 в runtime/updates/20260917-122011/engine-backup/llama.cpp. Устранено замедление FlashAttention в config/default.json (64x speedup, 52.8 t/s prompt eval, 59.2 t/s generation). scripts/maintenance.py status подтвердил: all_components_match: true (59 пакетов, 2 бэкенда).
EVIDENCE:
  - tools/downloads/llama-b10991-bin-win-cuda-12.4-x64.zip -> SHA-256 verified DE86232A73A0FD5B96454309DA2993F2DC6BF5E16ECC506192F551D49D833CDF.
  - scripts/test-engine-maintenance.ps1 -> staged install, forced swap, backup and rollback passed (ok: true).
  - scripts/update.ps1 -> update completed, backup at runtime/updates/20260917-122011/engine-backup/llama.cpp.
  - config/default.json -> removed --flash-attn on, set --cache-type-v q4_0 (prompt eval 0.25 t/s -> 52.8 t/s).
  - scripts/maintenance.py status -> {"all_components_match": true, "packages_match": true, "all_engine_backends_match": true}.
  - scripts/run_test_suite.py --order-audit -> passed 531 tests x4 runs (2124 executions), seeds 17, 73, 211, 0 warnings.
  - scripts/test-rag.py --enabled -> passed 5/5, report runtime/rag/latest.json.
  - scripts/check.ps1 (KSENIA_TEST_RAG=1) -> exit code 0, Doctor green, LAN 200, RAG 5/5, TTS->STT 100% recall.
  - Port checks (18080..18083, 8080) -> all inactive, reader_threads = 0.
OPEN_ISSUES:
  - Физическая приёмка C3 (непрерывный диалог) и C4 (фоновый поиск) на гарнитуре JBL Tour One M3 с Александром.
  - Git push блокируется локальным HTTP 407 прокси (требуются учетные данные прокси или прямое подключение к интернету для push).
  - Этап C7: Production sandbox заблокирован на уровне ПК (AMD-V/SVM выключен в UEFI, см. docs/SANDBOX-AUDIT.md).
NEXT_ACTION:    Перейти к пользовательской приёмке живых тестов с Александром (JBL Tour One M3, диалог, фоновый поиск).

