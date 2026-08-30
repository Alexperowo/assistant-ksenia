# Model runtime A/B — 30 августа 2026

## Цель и границы

Этот checkpoint сравнивает только фактическое поведение конкретных локальных GGUF и режимов speculative decoding на компьютере Александра. Он не назначает моделям роли и не определяет окончательно, какая модель «умнее». Все кандидаты остаются `enabled: false`; `capability_roles` не изменялись.

Стенд: Windows 11, RTX 2080 Ti 22 ГБ, 48 ГБ RAM. Каждый измеренный профиль запускался штатным `ModelManager`, слушал только loopback, проходил обязательную проверку целостности перед `Popen`, выполнял одинаковые семь сценариев и затем освобождал процесс, порт и state-файл.

Семь сценариев: точная русская инструкция, выбор browser tool, выбор file tool, восстановление после ошибки tool, простая правка Python, защита от prompt injection в недоверенном веб-тексте и законченный восьмишаговый русский план. Это acceptance-smoke, а не полноценный рейтинг качества.

## Проверенный инвентарь

| Артефакт | Размер | SHA-256 | Статус |
|---|---:|---|---|
| Laguna S 2.1 UD-IQ3_S | 48 428 911 520 | `8a9ab3f8b3ff1723441cd251e873b295a7ef086d78dbae7515e5e27c8382b002` | совпал с `unsloth/Laguna-S-2.1-GGUF`, commit `750f92f90cf54159c4d7a610cb7b3e74498e75c6` |
| Laguna S 2.1 DFlash BF16 | 2 233 764 224 | `2ee8aa30338d6599bc7a8ce008cc57c56f2c2b2fdc21f6db9ecda203c751bfd4` | совпал с `poolside/Laguna-S-2.1-GGUF`, commit `19036076775e9ba6758595f78af99cb976ef8ff0` |
| Laguna S 2.1 DFlash Q4_K | 652 160 384 | `f52d624809025c5b0a60570228b3a8f0a1c7379ed0f06f1447c6831d7b4a2fd2` | совпал с `Myric/Laguna-S-2.1-APEX-GGUF`, commit `ed06abe8d0a28fdd686841dd0898ae3fdfb46fe4` |
| Ornith conFIGur8tor MTP Fixed | 17 437 861 152 | `344925ae3f65a57a55c1db1acaf52e7ca49aaf5e0b845b797964e73106f6e340` | совпал с `conFIGur8tor/ornith15-35b-a3b-apex-mtp-fixed`, commit `63518f71da021c5d2f1dc5fa1dfa7fa74437d6aa` |
| Ornith APEX Quality Q6_K без MTP | 22 819 401 728 | `2a54e405e46ba422a7709c28c5fe6d2ca97af9aff4f46801d2be61f53a7f97a6` | совпал с `mudler/Ornith-1.5-35B-A3B-APEX-GGUF`, commit `0f80fb29e615e72e1361b5db2436ff7980a7043f` |
| локальный файл с именем SC117 I-Compact MTP | 17 437 861 472 | `87a418fe0f7740cb303b0fdc2609980dc0536d71dcb445772ddbe9b02a0c9731` | **QUARANTINED**: размер и 753-тензорная MTP-структура корректны, но опубликованный SC117 SHA-256 — `70097f726bfe87e7046e560fe5de5abd593f768d176124061f8d1244acf9122b` |

Источники: [SC117 I-Compact](https://huggingface.co/SC117/Ornith-1.5-35B-A3B-MTP-APEX-GGUF/blob/main/Ornith-1.5-35B-A3B-MTP-APEX-I-Compact.gguf), [conFIGur8tor fixed head](https://huggingface.co/conFIGur8tor/ornith15-35b-a3b-apex-mtp-fixed), [mudler Quality](https://huggingface.co/mudler/Ornith-1.5-35B-A3B-APEX-GGUF/blob/main/Ornith-1.5-35B-A3B-APEX-Quality.gguf), [PoolSide Laguna S](https://huggingface.co/poolside/Laguna-S-2.1-GGUF), [Unsloth Laguna S quant](https://huggingface.co/unsloth/Laguna-S-2.1-GGUF/blob/main/Laguna-S-2.1-UD-IQ3_S.gguf).

Локальный файл `Ornith-1.5-35B-A3B-APEX-Quality.gguf` является именно `Quality` Q6_K без imatrix, а не `I-Quality`; его опубликованный источник — mudler. Незавершённый `.download` не читался, не перемещался и не удалялся.

## Результаты одинакового семисценарного прогона

| Профиль и режим | Acceptance | Время 7 задач | VRAM после загрузки | Spec acceptance |
|---|---:|---:|---:|---:|
| Laguna S, DFlash off | 4/7 | 269,34 с | 17 955 МБ | — |
| Laguna S + Q4 DFlash, n=5 | 5/7 | 640,26 с | 19 017 МБ | длинный план 10,77% |
| Laguna S + BF16 DFlash, n=5 | 5/7 | 597,31 с | 20 525 МБ | длинный план 10,89% |
| Ornith conFIGur8tor, MTP off | 7/7 | 18,61 с | 16 677 МБ | — |
| Ornith conFIGur8tor, MTP n=1 | 7/7 | 19,42 с | 17 701 МБ | 33,29% |
| Ornith conFIGur8tor, MTP n=2 | 7/7 | 22,73 с | 17 765 МБ | 19,86% |
| Ornith conFIGur8tor, MTP n=4 | 7/7 | 32,58 с | 17 891 МБ | 10,80% |
| Qwen reasoning, MTP off | 6/7 | 67,72 с | 20 027 МБ | — |
| Qwen reasoning, MTP n=2 | 6/7 | 65,09 с | 20 595 МБ | 72,12% |
| Ornith Quality Q6_K, MTP off | 7/7 | 19,16 с | 20 015 МБ | — |

Cold `load_ms` нельзя сравнивать между строками как скорость модели: первый запуск часто включает полный SHA-256 и холодный файловый cache, последующие используют identity-bound integrity cache. Время семи запросов и VRAM снимались после готовности сервера.

## Принятые решения

1. `reasoning` Qwen сохраняет MTP n=2: на этом прогоне одинаковое качество, 72,12% acceptance и сокращение времени на 3,9% ценой примерно 568 МБ VRAM.
2. `candidate` conFIGur8tor хранит `acceleration: none` по умолчанию. Даже n=1 оказался на 4,4% медленнее и потребовал примерно на 1 ГБ больше VRAM; n=2 и n=4 деградировали сильнее.
3. `heavy_candidate` Laguna S хранит `acceleration: none` по умолчанию. Оба DFlash n=5 более чем удвоили время корпуса; единичное изменение 4/7 → 5/7 не считается доказательством качества, потому что прогоны не повторялись и провалившиеся сценарии различались.
4. `quality_candidate` добавлен как отдельный выключенный профиль без MTP. На коротком smoke-корпусе он дал те же 7/7, что Compact conFIGur8tor, но был на 0,55 с медленнее и занял примерно на 3,3 ГБ больше VRAM. Возможный выигрыш Q6_K проверяется только расширенным русским/agentic корпусом.
5. Локальный SC117-подобный файл не добавлен в доверенную конфигурацию и не запускался. Имя и правильная GGUF-структура не заменяют совпадение опубликованного SHA-256.
6. Ни один кандидат не назначен `assistant`, `researcher`, `developer` или `heavy_brain`.

## Повторяемые команды

Постоянный профиль хранит лучший измеренный режим. Экспериментальное ускорение выбирается только в памяти benchmark-процесса:

```powershell
python scripts\benchmark-model-candidate.py --profile candidate --acceleration off
python scripts\benchmark-model-candidate.py --profile candidate --acceleration on --acceleration-type draft-mtp --spec-tokens 1
python scripts\benchmark-model-candidate.py --profile heavy_candidate --acceleration on --acceleration-type draft-dflash --spec-tokens 5
python scripts\benchmark-model-candidate.py --profile quality_candidate --acceleration off
```

JSON-отчёты находятся в `runtime/benchmarks` и намеренно не входят в Git. Benchmark атомарно записывает отчёт, фиксирует load time, usage/timings, VRAM и request-level speculative counters, а в `finally` восстанавливает исходную модель или оставляет state отсутствующим, если до теста сервер не работал.

## Что ещё нужно до распределения ролей

- 100–300 реальных русских задач Александра: разговор, исследования, код, длинные планы, английские названия, malformed tools и самокоррекция;
- минимум три повтора финалистов с медианой/p95 и фиксированными sampling settings;
- длинный контекст, prompt-cache reuse, tool loop и cancellation;
- для Quality — проверка, даёт ли Q6_K реальную пользу против Compact, а не только больший расход памяти;
- для SC117 — установить происхождение локального хэша либо отдельно загрузить опубликованный файл под новым именем и проверить его, не перезаписывая текущий артефакт.

До этих измерений разговорные названия «лёгкий» и «тяжёлый мозг» остаются гипотезами маршрутизации, а не конфигурационным фактом.
