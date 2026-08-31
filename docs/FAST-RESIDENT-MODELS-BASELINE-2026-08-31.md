# Быстрая резидентная пара: baseline 2026-08-31

## Цель

Проверить, можно ли держать UI-модель и быстрого web-исследователя одновременно, а также подобрать им полезный режим рассуждения без замены основного контура Laguna/Qwen и без обхода Permission Broker.

Изменение принималось только после живых измерений. На момент этого baseline функциональные роли ещё не переключались: сначала были внедрены управляемый runtime, request modes, строгий протокол предложения UI-действия и независимая проверка. Последующий web-research rollout описан отдельно в `FAST-RESEARCH-ROUTING-2026-08-31.md`.

## Закреплённые артефакты

| Профиль | Источник GGUF | Commit | Размер | SHA-256 |
|---|---|---|---:|---|
| `ui_butler` | `bartowski/tencent_UI-Mate-9B-GGUF`, `tencent_UI-Mate-9B-Q4_K_M.gguf` | `695da95fcfcb1fed85e9b4dd6d698e3575f09288` | 5 910 783 104 | `a54257e...af23e2c` |
| projector | тот же репозиторий, `mmproj-tencent_UI-Mate-9B-f16.gguf` | тот же | 918 166 016 | `5a8380c...5d2c16` |
| `research_fast` | `bartowski/InternScience_Agents-A1-4B-GGUF`, `InternScience_Agents-A1-4B-Q4_K_M.gguf` | `41e8e3a6b8406cfbffb7d11165c6e49057abf79d` | 2 884 851 648 | `3712f3a...68d90e7` |

Полные хеши и исходные имена находятся в `config/default.json`. Runtime снова проверяет полный размер и SHA-256 перед первым штатным запуском; локальный путь определяется через настраиваемые model roots.

## Runtime

Обе модели используют официальный закреплённый `llama.cpp` b10621, 16K и полный GPU offload. Сервисы независимы:

- `ui_fast`: `127.0.0.1:18082`, `runtime/state-ui-fast.json`;
- `research_fast`: `127.0.0.1:18083`, `runtime/state-research-fast.json`.

Они используют тот же локальный API key contract, CORS restriction, проверку PID/executable, lock артефактов и отказ при неизвестном владельце порта, что и основной сервис. Совпадающие endpoint или state-файлы конфигурация отклоняет до запуска.

Штатный `ResidentModelPool` поднял обе модели за 17,2 секунды; фактический контекст обеих — 16 384. При одновременной загрузке использовано 11 143 MiB из доступных 22 528 MiB GPU memory, осталось 11 088 MiB. Два коротких параллельных запроса заняли 0,821 и 0,800 секунды; во время vision-запроса UI-Mate — 13,587 и 1,397 секунды соответственно.

Это подтверждает совместное проживание именно малой пары. Инвариант «одна крупная модель» сохраняется: последующий `ModelResidencyCoordinator` уже проверен на освобождении пары и точном восстановлении после primary window.

## UI-Mate

Измерения на закреплённых официальных reference screenshots проводились без исполнения действий:

| Сценарий | Результат |
|---|---:|
| русский text-only chat | 1,25 с |
| обычный screen Q&A | 12,35 с |
| vision action, 2048 image tokens, cold | 33,1 с |
| тот же прогретый prefix | 4,85 с |
| `image-max-tokens=1024`, новые изображения | 13,2–19,5 с |

При 1024 image tokens сохранились правильные действия для VS Code Extensions и Chromium Ctrl+D. Штатный proposer на новом защищённом сервисе вернул для VS Code `left_click [49,280]`; действие было только разобрано и проверено, но не исполнено.

Нативный thinking sweep дал нестабильный результат:

- 64 tokens: VS Code верно, Chromium ошибочно отвлёкся на уведомление;
- 128 tokens: VS Code/Chromium верно, Spotify ушёл в терминал;
- 256 tokens и unrestricted: Spotify снова выбрал терминал.

Следовательно, `DELIBERATE` для UI-действия не означает «дать UI-Mate больше токенов». Это `enable_thinking=false` у proposer-а, независимая policy-проверка Agents-A1 и максимум одна обратная коррекция.

## Agents-A1

`FAST` использует `enable_thinking=false`. Точный русский ответ получен за 0,401 секунды; корректный `browser_search` tool call — примерно за 1,47 секунды. Модель не выполнила запрещённый tool из prompt-injection примера.

Для `DELIBERATE` проверены budgets 64/128/256/512:

- 128 нестабилен на конфликтующих источниках: reasoning мог попасть в content;
- 256 с отдельным transition message завершает краткий финал и сохраняет reasoning отдельно;
- 512 провоцировал циклическое рассуждение, 25,2 секунды и исчерпание лимита.

Принято: 256 reasoning tokens и `Now provide the concise final answer in the requested format.`. FAST/DELIBERATE на штатном клиенте дали одинаково верный ответ `323`; deliberate дополнительно вернул отдельный `reasoning_content`.

## Перекрёстная проверка

Контрольные действия:

| Кандидат UI-Mate | Решение Agents-A1 |
|---|---|
| открыть Extensions в VS Code | approve |
| Ctrl+D в Chromium | approve |
| открыть терминал для установки Spotify через native GUI | reject, использовать Ubuntu Software/Activities |

Самопроверка UI-Mate отвергнута: та же модель одобрила собственный ошибочный переход в терминал. Перекрёстная проверка обнаружила ошибку; после её feedback UI-Mate выбрала Ubuntu Software и корректный координатный шаг.

Production-модуль `ui_deliberation.py` ничего не исполняет, строго разбирает JSON reviewer-а и fail-closed останавливается при повреждённом ответе. `ui_mate.py` принимает ровно один XML action/tool call, нормализует только разрешённые параметры, отвергает неизвестный tool, повтор параметра, лишний текст и координаты вне 0–999. Результат остаётся `ActionProposal`, а не Python-кодом.

## Ограничения и следующий gate

- Agents-A1 выполняет policy/semantic review, но не видит screenshot и не подтверждает точность координат.
- Нет production ScreenCaptureService с явными bounds виртуального рабочего стола и multi-monitor mapping.
- `ActionProposal` ещё не передаётся исполнителю. Будущий adapter обязан преобразовать его только в существующие `windows_*` tools, после чего Permission Broker снова применит `windows_write=confirm` и financial guard.
- В DELIBERATE проверяется каждый шаг, а не только первый. Иначе безопасный `Activities` может следующим шагом превратиться в поиск терминала.
- Две малые модели не запускаются рядом с большой: coordinator и измерение освобождения/восстановления добавлены следующим чекпоинтом.

Следующая UI-приёмка: реальный Windows screenshot capture, отрицательные multi-monitor/DPI тесты, 20–30 read-only UI proposals, затем один подтверждённый безопасный action через существующий Permission Broker. Agents-A1 уже принят отдельно для web research; UI-профиль остаётся read-only experimental.
