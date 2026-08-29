# Карта исходного кода

Крупные модули пока намеренно не разбиваются механически: сначала завершится пользовательская приёмка. Таблица показывает ответственность и ближайшие связи, чтобы новый исполнитель не искал вслепую.

## Ядро и запуск

| Файл | Ответственность | Связан с |
|---|---|---|
| `cli.py` | Команды, меню, текстовый и голосовой интерфейсы, локальное включение доверенной задачи | почти все сервисы; постепенно декомпозировать |
| `config.py` | deep-merge default/user, typed engine/model/draft/projector profiles, acceleration, reasoning и атомарная запись личных overrides | `default.json`, тесты конфигурации |
| `doctor.py` | проверка среды, моделей, памяти, GPU и баз | `model_manager`, хранилища |
| `atomic_io.py` | атомарная запись/копирование, fsync и межпроцессные file locks | persistent JSON, журналы, диагностика и файловые транзакции |
| `schema_validation.py` | строгая проверка вложенных аргументов tools до Permission Broker и executor | `agent`, `tools` |
| `instance_lock.py` | запрет второго экземпляра критического интерфейса | CLI/LAN/voice |
| `user_messages.py` | безопасные короткие сообщения об ошибках | CLI и голос |

## Модели и агентность

| Файл | Ответственность | Критические инварианты |
|---|---|---|
| `model_manager.py` | одна активная LLM, декларативный выбор backend-а, команда DFlash/MTP/mmproj, PID, порт и целостность всех нужных GGUF | неизвестный процесс не завершать; loopback; API-key; bounded wait; size + полный SHA-256 до первого Popen; cache только для неизменной file identity |
| `chat.py` | OpenAI-совместимый HTTP, stream, tokenizer, system normalization и отменяемое чтение ответа | один первый system; timeout; локальный ключ; checkpoint даже при зависшем socket read |
| `agent.py` | цикл tool calling одной модели | общий лимит шагов/инструментов/вопросов подтверждения, checkpoint отмены |
| `orchestrator.py` | выбор роли, планирование и исполнение разными моделями | планировщик только читает; handoff сохраняется |
| `model_catalog.py` | безопасное перечисление GGUF | только `models_dir` и `model_search_dirs`, относительные пути от корня проекта |
| `model_assets.py` | закреплённое происхождение, загрузка и SHA-256 GGUF | полный commit; безопасное имя; без ambient token |
| `model_evaluation.py` | русский A/B и критерии кандидата | детерминированные проверки, недоверенные данные |

## Инструменты и безопасность

| Файл | Ответственность |
|---|---|
| `tools.py` | схемы и исполнение файлов, браузера, Windows, памяти, RAG; целиковая запись только создаёт новый путь, существующий текст меняется точной однозначной заменой |
| `permissions.py` | политика allow/confirm/deny и неослабляемый минимум безопасности |
| `approval.py` | область и повторное использование подтверждения |
| `confirmation.py` | понятное описание конкретного действия |
| `trusted_task.py` | локальный одноразовый жетон следующей задачи, срок, атомарное потребление и статусы |
| `developer.py` | fail-closed выбор command backend, проверка argv/workspace и явно небезопасный legacy host-runner | неизвестный backend не запускается; `unsafe_host` требует точного признания риска; shell запрещён |
| `journal.py` | сериализованная транзакция mutation + undo-record, резервная копия, SHA-256 и безопасная отмена файлов |
| `sensitive_data.py` | запрет секретных путей и расширений |
| `processes.py` | идентификация и подтверждённое завершение Windows-процесса |
| `procedures.py` | чтение проверенных процедур без traversal |

## Интернет и Windows

| Файл | Ответственность |
|---|---|
| `browser.py` | родительский безопасный API дочернего Chromium |
| `research.py` | запрос, выбор источников, параллельное чтение в стабильном порядке и синтез |
| `windows_automation.py` | UI Automation высокого уровня |
| `windows_bridge.py` | окна, клавиатура, указатель и низкоуровневый Win32 |
| `scripts/browser_worker.py` | Chromium, поиск и SSRF/redirect guard в отдельном процессе |
| `scripts/windows_uia_worker.py` | изоляция потенциально зависающей UI Automation |

## Голос

| Файл | Ответственность |
|---|---|
| `wake.py` | Vosk listener для активации/остановки и явная передача единственного микрофона голосовому подтверждению |
| `stt.py` | управление faster-whisper, Vosk-partial callbacks и записью команды |
| `speech.py` | очередь TTS, подтверждённый ready/error Silero worker, SAPI-резерв, сериализованная остановка и callback полного завершения фразы |
| `live.py` | независимая state machine Live, streaming TTS, barge-in и разделение generated/spoken; cancellation event ставится до audio stop, произнесённым считается только непрерывный завершённый префикс |
| `turn_detection.py` | чистое накопление Vosk partial/final сегментов и hybrid turn detector по транскрипту, VAD и времени тишины |
| `speech_text.py` | русское произношение чисел, дат и времени |
| `media_buttons.py` | AVRCP/медиакнопка как опциональная активация |
| `resilience.py` | bounded backoff повторяющихся ошибок |
| `scripts/audio_input.py` | выбор и открытие реального аудиовхода |
| `scripts/wake_worker.py` | дочерний процесс Vosk |
| `scripts/stt_worker.py`, `stt_service.py` | дочерний/долгоживущий faster-whisper; в opt-in Live сервис дополнительно использует закреплённый Vosk для partial endpointing |
| `scripts/voice_worker.py` | Silero-синтез и WAV |
| `scripts/pcm_audio.py` | совместимость PCM/audioop на Python 3.12 |

## Память и задачи

| Файл | Ответственность |
|---|---|
| `memory.py` | короткая история и сжатая сводка |
| `knowledge.py` | подтверждённые долговременные факты SQLite |
| `handoff.py` | план, запрос, ошибки и результат между ролями |
| `rag.py` | FTS5, векторы, chunking, RRF, порог смыслового совпадения и цитаты строк |
| `embeddings.py` | временный CPU `llama-server` для embeddings |
| `tasking.py` | долговечное состояние, пауза, отмена, восстановление и раздельные generated/spoken ответы Live |

## Интерфейсы и диагностика

| Файл | Ответственность |
|---|---|
| `lan.py` | HTTP/PIN/session/rate limit и связь с задачей |
| `web/*` | доступная визуальная LAN-панель |
| `diagnostics.py` | безопасный JSONL, scrub и ротация |
| `diagnostic_report.py` | сводка событий без раскрытия текста |
| `performance.py` | чтение ротированного JSONL, trace waterfall, completeness и статистики p50/p95 |
| `scripts/performance-report.py` | человеко- и машиночитаемый отчёт реальных задержек |
| `scripts/test_model_cancellation.py` | живой gate отмены LLM-stream и фактического завершения HTTP reader |
| `scripts/check.ps1` | объединённый quality gate |
| `scripts/run_test_suite.py` | инвентарь, warnings, skips и shuffled order |

## Установка и выпуск

| Файл | Ответственность |
|---|---|
| `install-runtime.ps1` | Python, точные пакеты, browser и speech pack |
| `install-llama.ps1` | проверенная стадийная установка движка |
| `install-local-backend.ps1` | стадийная установка закреплённого локально собранного backend-а по runtime manifest |
| `update.ps1` | применение только одобренных lock-версий |
| `rollback-update.ps1` | возврат последней сохранённой установки |
| `hardware-report.ps1` | read-only инвентарь и стартовый профиль |
| `build-release.ps1` | чистый архив без моделей и личных данных; по умолчанию только из чистого Git-дерева |
| `validate_release.py` | машинная проверка исходного дерева/архива |
| `scripts/model-assets.py` | явная загрузка/проверка закреплённого GGUF вне автоматической установки |
| `scripts/download-model-assets.ps1` | общий PowerShell-вход загрузки профиля или отдельного артефакта |
| `scripts/runtime-paths.ps1` | единый переносимый resolver Python 3.12 без фиксированных дисков |

## Где находятся тесты

Имена `tests/test_*.py` следуют имени подсистемы. Живые проверки лежат в `scripts/test-*.py`, `scripts/test-*.ps1` и benchmark-скриптах. Не заменяйте живой тест моками только ради процента покрытия.
