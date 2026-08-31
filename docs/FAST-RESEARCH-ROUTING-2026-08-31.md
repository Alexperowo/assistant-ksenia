# Быстрый web research и residency coordinator: приёмка 2026-08-31

## Цель и границы

Подключить Agents-A1 к реальному web-research контуру, не допустить одновременную загрузку малой пары и Laguna/Qwen и сохранить возможность выбирать модель/режим только конфигурацией. UI-Mate на этом этапе не получает право исполнять действия: он лишь остаётся второй моделью резидентной пары.

## Реализованный контракт

- `capability_roles.researcher.primary_model` указывает на `research_fast`.
- Endpoint берётся из service профиля выбранной роли; web research больше не предполагает primary port.
- `runtime_routing.research_request_modes` задаёт режим отдельно для `query`, трёх вариантов `synthesis` и `verification`. Для другого model profile mapping может отсутствовать — тогда сохраняется его обычный API-контракт.
- FAST: query planning и express synthesis.
- DELIBERATE: normal/deep synthesis и deep verification. У закреплённого Agents-A1 это `enable_thinking=true`, server budget 256 и явный transition message.
- `ModelResidencyCoordinator` взаимоисключает primary и residents, проверяет закрытие каждого порта и не завершает неизвестный процесс.
- Primary window восстанавливает ровно тот набор residents, который был активен до задачи. Пустой набор не приводит к неожиданному запуску малых моделей.
- Частичный отказ start/stop откатывает только доказанно затронутые роли.

## Живая проверка residency

На рабочей машине выполнен цикл:

1. `activate_residents()` поднял `ui_butler` и `research_fast`, оба с фактическим контекстом 16 384.
2. `suspend_residents_for_primary()` вернул lease `("ui_butler", "research_fast")`; оба порта закрылись.
3. `restore_after_primary()` запустил обе роли с новыми PID; оба API снова были ready.
4. `stop_all()` завершил только управляемые процессы.

Полный цикл занял 23,188 с. После него на 18080–18083 не осталось listeners, в `runtime/models` — state-файлов, в системе — управляемых `llama-server`.

## End-to-end web baseline

Оба запроса использовали реальный browser search/read и официальный профиль Agents-A1. Время включает cold запуск резидентной пары.

| Сценарий | Режим | Итоговое время | Наблюдение |
|---|---|---:|---|
| найти официальный релиз Python 3.12.10 и дату | express / FAST | 25,531 с | точный официальный URL и дата; research pipeline без cold load — 16,296 с |
| проверить релиз, дату и статус поддержки | normal / FAST query + DELIBERATE synthesis | 55,500 с | корректные основные факты; research pipeline без cold load — 46,297 с |

Normal trace:

- запуск UI-Mate: 5,078 с;
- запуск Agents-A1: 3,562 с;
- FAST query generation: 1,172 с, 84 completion tokens, 71,672 token/s;
- два поиска: 2,469–2,922 с параллельно;
- три страницы: 2,812–4,890 с параллельно;
- DELIBERATE synthesis: 37,265 с, 466 completion tokens, 1100 символов отдельного reasoning, 12,505 effective token/s.

Это acceptance samples, а не статистический benchmark. Они доказывают правильность маршрута и порядок задержки, но не заменяют corpus с p50/p95.

## Качество и известные ограничения

- Официальный Python URL и дата были получены верно в обоих случаях.
- Normal-ответ корректно назвал переход ветки 3.12 от bugfix-релизов к security-only обновлениям, хотя русская формулировка «последний полный выпуск поддержки» требует стилистической доводки.
- Помимо официального источника модель указала явно помеченное зеркало. Для запроса с требованием только official domains это нежелательно; нужен corpus-тест authoritative-source filtering, а не специальное условие для Python.
- Холодный запуск обеих моделей добавляет около 9 секунд в текущем последовательном запуске. При постоянной резидентности эта часть исчезает.
- DELIBERATE normal существенно медленнее FAST, но остаётся в пределах десятков секунд, а не прежних многоминутных ожиданий. Понижать budget ниже 256 без нового A/B нельзя: предыдущий sweep показал нестабильность 128.
- UI-Mate запускается вместе с исследователем намеренно как стабильная пара. Динамическое удаление tools/models внутри одной задачи не вводится.

## Проверки и откат

Добавлены regressions на порядок primary stop → residents start, unknown port owner, rollback частичного suspend, exact-set restore, обязательный primary residency window, выбор endpoint и request mode для research stages и запрет неизвестного stage. Быстрый gate: 393/393 без warnings.

Откат не требует изменения Python: вернуть `capability_roles.researcher.primary_model` на нужный primary profile и убрать/заменить его stage mapping. Coordinator останется безопасным неактивным механизмом. Нельзя откатываться к запуску resident-профиля через primary manager или к одновременной загрузке primary и малой пары.

## Следующий шаг

Собрать 20–30 реальных web-запросов владельца по режимам express/normal/deep, измерить p50/p95, качество источников и command error rate. Только после этого оптимизировать evidence size, source ranking или deliberate budget. UI-ветка продолжается отдельно: ScreenCaptureService, multi-monitor/DPI bounds и read-only corpus до любого физического клика.
