OBJECTIVE:      Надёжность голосового ассистента Ксения (C0–C8), приёмка этапов и перевод в повседневное использование
PHASE:          C1/C5/C6 (HTTP-транспорт защищён; переход к C5/C6) / 9 этапов (C0–C8)
STATE:          Все 530/530 тестов пройдены успешно (0 ошибок, 0 предупреждений). 4 прогона (1 прямой + 3 случайных порядка seed 17, 73, 211 = 2120 тестов) успешны. Полный проверочный скрипт scripts/check.ps1 с активным RAG (KSENIA_TEST_RAG=1) пройден с кодом 0 (Doctor green, LAN 200, RAG 5/5 live green, TTS->STT 100% recall). Все локальные порты (18080–18083, 8080) свободны.
DONE:
  - C0: Первичная инвентаризация и исходный воспроизводимый результат (commit 7cbcc74).
  - C1: Отказоустойчивость сессии и очистка TaskCancelled при 4 типах отказов (commit 9ea72c1).
  - C2: Разбор Thinking/Fast, авто-бюджеты исследований, изоляция пользовательских настроек (commits 70822f1, 37e83d2, 0e4356d).
  - C3: Непрерывный живой голосовой диалог, таймаут тишины 8 с, голосовые команды («Замолчи», «Отбой», «Выход») (commit f5cfabb).
  - C4: Фоновый веб-поиск на Agents-A1 параллельно разговору с UI-Mate, BackgroundResearchManager, изоляция сессии, речевой диспетчер (commit 97261ae).
  - C8 (RAG): Загружена закреплённая модель Qwen3-Embedding-0.6B-Q8_0.gguf (639 150 592 байт, SHA-256 06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439), выполнен живой запуск эмбеддера на CPU (порт 18081), пройдена инкрементальная индексация workspace и 5/5 поисковых сценариев с FTS5 + векторным поиском + RRF и отсечением no_match (commit 00d00c4).
  - C1+ HTTP Audit: Защита HTTP-транспорта stream_chat и count_chat_tokens через _open_completion_response (loopback enforce, bypass proxy, cancellation check при ожидании headers, exc.close() при HTTPError, 6 новых live-socket тестов, commit 202ce5c).
EVIDENCE:
  - scripts/test-rag.py --enabled -> passed 5/5, report runtime/rag/latest.json.
  - scripts/run_test_suite.py --order-audit -> passed 530 tests x4 runs (2120 executions), seeds 17, 73, 211, 0 warnings.
  - scripts/check.ps1 (KSENIA_TEST_RAG=1) -> exit code 0, Doctor green, LAN 200, RAG 5/5, TTS->STT 100% recall.
  - Port checks (18080..18083, 8080) -> all inactive, reader_threads = 0.
OPEN_ISSUES:
  - Физическая приёмка C3 (непрерывный диалог) и C4 (фоновый поиск) на гарнитуре JBL Tour One M3 с Александром.
  - Git push 9 коммитов блокируется прокси HTTP 407 (требуются учетные данные прокси или прямое соединение).
  - Этап C7: Production sandbox заблокирован на уровне ПК (AMD-V/SVM выключен в UEFI).
  - Этап C5: Поэтапное обновление компонентов (llama.cpp b10834 / PoolSide laguna, Python, Playwright).
  - Этап C6: Добавление недостающих меток (milestones) в performance harness для фоновых задач.
NEXT_ACTION:    Перейти к этапу C5/C6 (анализ версий runtime-lock и milestones производительности).

