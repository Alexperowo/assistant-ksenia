# ScreenCaptureService: physical DPI baseline 2026-08-31

## Подтверждённая проблема

До изменения в проекте существовали Win32 virtual-desktop bounds для проверки мыши и отдельный vision-клиент, принимающий готовый PNG. Единого объекта, связывающего снимок с геометрией мониторов, не было.

На фактическом компьютере обычный Win32 вызов в DPI-unaware процессе вернул `[0, 0, 1920, 1080]`, а Pillow `ImageGrab(all_screens=True)` — изображение 3840×2160. Причина — масштаб Windows 200%. Использовать logical bounds для физического PNG небезопасно: координаты UI-Mate были бы смещены вдвое.

## Контракт

`ScreenCaptureService`:

- использует thread-scoped `DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2` и восстанавливает прежний контекст;
- перечисляет каждый monitor rectangle, work area, primary flag и effective DPI;
- требует ровно один primary monitor и совпадение union monitor bounds с physical virtual metrics;
- получает весь desktop через `ImageGrab(all_screens=True, include_layered_windows=True)`;
- требует точного совпадения исходного изображения и desktop bounds;
- при необходимости уменьшает только передаваемый vision PNG с сохранением пропорций, не меняя physical mapping;
- не пишет screenshot на диск и журналирует только размеры, число мониторов, длительность и объём PNG;
- перед будущим действием умеет повторно проверить неизменность monitor/DPI layout;
- переводит координаты 0–999 в physical pixels с точным попаданием крайних значений;
- отклоняет точку, если она попала в промежуток между мониторами или в перекрытие rectangles.

## Живая приёмка

| Поле | Значение |
|---|---|
| virtual desktop | `[0, 0, 3840, 2160]` |
| encoded PNG | 3840×2160, 981 148 байт |
| monitor | `DISPLAY1`, primary |
| work area | `[0, 0, 3840, 2064]` |
| effective DPI | 192×192 |
| normalized `(0,0)` | physical `(0,0)` |
| normalized `(999,999)` | physical `(3839,2159)` |

Затем выполнен полный read-only контур на текущем экране. UI-Mate предложила `left_click [14,971]` по видимой кнопке Start; Agents-A1 одобрил нативный GUI-шаг; mapping дал physical `(54,2098)`. Поле результата `executed=false`; Windows mouse/keyboard APIs не вызывались. Весь cold цикл с моделями занял 19,75 с, после чего оба model services штатно остановлены.

## Что ещё не доказано

- Физически подключён один монитор; negative origin, разные DPI и gaps проверены пока детерминированными unit-cases, а не реальной multi-monitor установкой.
- Layout validation видит смену мониторов/DPI, но не перестановку содержимого окна после screenshot. Будущий executor должен дополнительно ограничить возраст кадра и перепроверить target window.
- Один Start case не подтверждает общую визуальную точность UI-Mate.
- Reviewer оценивает семантику и политику, но не видит screenshot и не утверждает правильность координат.
- Ни `ScreenCaptureService`, ни UI-Mate proposal не соединены с `windows_*` executor. Это намеренная граница до corpus.

Следующий gate: 20–30 read-only случаев на Windows Settings, Explorer, браузере, VS Code и системных диалогах, включая масштаб/окна/уведомления. Для каждого фиксируются ожидаемый action/region, фактический proposal, review, latency и ошибка координат. Только после зелёной приёмки допускается один подтверждённый action через существующий Permission Broker.
