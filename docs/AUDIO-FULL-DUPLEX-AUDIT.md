# Аудит AudioCaptureService, AEC и full-duplex

Обновлено: 30 августа 2026 года.

## Вывод

Существующий Live-контур нельзя переписывать: state machine, streaming TTS, hybrid endpointing, barge-in ordering и разделение `generated_text`/`spoken_text` уже реализованы и покрыты тестами. Незакрытая часть находится ниже этого слоя — во владении физическим аудиоустройством и синхронизации near/far PCM.

В обычном `voice-agent` теперь работает единый `AudioCaptureService`. Один дочерний процесс открывает физический input stream и публикует mono/int16 PCM по 10 мс через ограниченные очереди. `wake_worker.py`, `stt_service.py`, Vosk fallback и busy stop-monitor подключаются как потребители к одному локальному потоку:

1. Родитель запускает capture worker и получает случайный 256-битный ключ только в памяти.
2. Worker слушает только loopback; ключ передаётся дочерним потребителям через environment, не попадает в argv, repr или журнал.
3. Каждый подписчик получает только будущие 10-мс кадры; его очередь ограничена двумя секундами и при отставании отбрасывает старейшие кадры.
4. `MicrophoneCaptureGate` по-прежнему сериализует stop-monitor и голосовое подтверждение, но переключает подписчиков, не переоткрывая Bluetooth endpoint.
5. Если общий worker не запустился, старый локальный путь остаётся безопасным fallback и сохраняет прежний gate.

Это устраняет повторное открытие input внутри штатного голосового сеанса и создаёт общую монотонную сетку near-end кадров. Это ещё не полный full-duplex: произвольный речевой barge-in не подключён, а TTS пока не публикует far-end PCM.

TTS-сервис синтезирует WAV, затем отдельный PowerShell-процесс проигрывает его через `System.Media.SoundPlayer`. Родитель видит software-события начала/конца, но capture path не получает тот PCM, который реально поступил в render buffer. Поэтому подключить AEC3 только перед VAD недостаточно: far reference отсутствует и не синхронизирован.

## Статус компонентов

| Компонент | Статус | Доказательство и граница |
|---|---|---|
| `LiveSession` и streaming TTS | DONE | `src/butler/live.py`; FIFO completion и late-callback тесты |
| Generated/spoken text | DONE | только непрерывный полностью проигранный prefix попадает в память |
| LLM cancellation | DONE | живой Laguna/PoolSide gate, 500–657 мс до отмены |
| Keyword/headset barge-in | PARTIAL | stop monitor работает, но распознаёт grammar wake/stop, а не произвольную речь |
| Hybrid turn detection | DONE для записываемой команды | Vosk partial + amplitude gate + semantic timing; не работает постоянно во время TTS |
| Единоличное владение устройством | DONE для `voice-agent` | один capture worker владеет endpoint; wake/STT/stop переключают подписки, fallback сохраняет прежний gate |
| `AudioCaptureService` / ring buffer | DONE для near-end | loopback + случайный ключ, точные 10-мс mono/int16 frames, bounded drop-oldest queues и обнаружение разрыва |
| AEC | MISSING | нет системной проверки APO и нет WebRTC far reference |
| Noise suppression / AGC | MISSING | amplitude noise gate не является NS/AGC |
| Input/output device profile | PARTIAL | строковый `wake_device` теперь можно доступно и атомарно выбрать по устойчивому фрагменту имени; пары endpoint id/output/echo delay ещё нет |
| Device calibration | MISSING | нет корреляционного измерения render→capture delay |

## Подтверждённая проблема выбора устройства

На текущей Windows PortAudio вернул `default_input=-1`. При этом найдено несколько WDM-KS входов, включая не только микрофоны, но и системный mix/line input. Старый алгоритм в таком состоянии мог открыть первый работоспособный вход и принять системный звук за речь.

Минимальное исправление сделано fail-closed: если selector пуст, default input отсутствует и входов несколько, Ксения просит выбрать `voice.wake_device` и не открывает произвольное устройство. Один-единственный вход по-прежнему выбирается автоматически. Ярлык списка микрофонов принимает уникальную часть имени, валидирует её по текущему списку и атомарно сохраняет личную настройку; неоднозначность и отсутствие совпадения не меняют конфигурацию. PortAudio index намеренно не сохраняется. Имена JBL, индексы и пути машины в код не добавлены.

Живая проверка 30 августа 2026 года на подключённой Bluetooth-гарнитуре открыла default MME, явный WASAPI и WDM-KS без ошибок, но все три пути дали почти цифровой ноль (`peak 0`, `58` и `37` при пороге `220`). Новый общий worker затем отдал подписчику 200 точных кадров `320 bytes / 16 kHz` и штатно закрылся, но сигнал снова имел только `peak=5`. Следовательно, текущий отказ произошёл до Whisper и не доказывает дефект STT. До проверки индикатора входа Windows, аппаратного mute и Bluetooth multipoint менять приоритет host API или пороги нельзя.

Настоящие desktop-ярлыки запуска и остановки также пройдены. START создал валидный state, один физический capture worker, постоянный faster-whisper CUDA service и wake subscriber на одном loopback port. STOP удалил state и весь процессный подграф без оставшегося аудиосервиса.

## AEC-кандидаты

### Windows communications AEC

Microsoft sample требует Windows build 22540+; текущая машина имеет build 26200. Однако API предоставляет AEC только communications capture stream, для которого драйвер/APO действительно объявил `AUDIO_EFFECT_TYPE_ACOUSTIC_ECHO_CANCELLATION`. Сам номер build этого не гарантирует. Текущий SoundPlayer/capture path не создаёт и не проверяет такой stream, поэтому системный вариант остаётся экспериментом, а не production backend.

Источник: `microsoft/Windows-classic-samples`, `Samples/AcousticEchoCancellation`.

### `pywebrtc-audio` 0.1.0

Проверен Windows CPython 3.12 wheel:

- версия: `0.1.0`, статус upstream: beta;
- PyPI Trusted Publishing source commit: `9c1c2a2186bb05244d39ac2b906edc3ec4328463`;
- wheel: `pywebrtc_audio-0.1.0-cp312-cp312-win_amd64.whl`;
- размер: `311988` байт;
- SHA-256: `6EEF9065089A2E25EF1B9743661CB48CAA4C2E65ACEE1314E8957D0C24AE534A`;
- единственная runtime-зависимость: `numpy>=1.24`;
- 129 из 129 upstream-тестов прошли из изолированного `--target`, без установки в voice venv;
- на текущем Ryzen 100 мс AEC+NS+AGC обрабатываются примерно за 1,04 мс, около 96× real-time.

Ограничения: проект молодой, Windows `.pyd` не имеет Authenticode-подписи. Библиотека не захватывает и не проигрывает звук; `AudioProcessor.process(near, far)` требует одинаково выровненные PCM-буферы. Поэтому wheel пока не добавлен в production lock и не установлен в рабочее окружение.

Артефакты исследования лежат только в исключённом из релиза `runtime/security-audit/pywebrtc-audio-0.1.0`.

## Минимальная последовательность реализации

1. **Device contract — DONE для input.** Хранится не индекс, а пользовательское предпочтение и фактически открытый endpoint; неоднозначность даёт fail-closed. В журнале есть input endpoint, sample rate, frame size и startup latency без записи звука.
2. **Единый near-end worker — DONE для `voice-agent`.** Один процесс владеет input stream и публикует 10-мс frames в ограниченные очереди. Wake, partial/final STT и stop-monitor являются потребителями. Старый путь сохранён как fallback.
3. **Render reference.** Перевести Live playback на backend, который принимает PCM Silero и одновременно кладёт фактически отправленные render frames в тот же audio worker. Нельзя считать момент генерации WAV моментом воспроизведения.
4. **AEC adapter.** Подключить backend через узкий интерфейс. Сначала offline synthetic corpus, затем реальный A/B Windows APO против закреплённого WebRTC wheel. NS и AGC включать отдельно и сравнивать STT, чтобы не ухудшить окончания.
5. **Calibration profile.** Только если измерения показывают пользу: `input_endpoint`, `output_endpoint`, sample rates, estimated echo delay и AEC settings. При смене устройства профиль не переиспользовать вслепую.
6. **Physical acceptance.** Наушники, затем JBL-динамики: 20–30 ходов, паузы 1–2 секунды, произвольный barge-in, отсутствие self-interrupt, `interrupt_detected → audio_actually_stopped`, корректный spoken prefix.

`live.enabled` остаётся `false` по умолчанию, пока пункты 3–6 не приняты на реальном устройстве. Установка DSP-библиотеки сама по себе не считается готовым AEC.
