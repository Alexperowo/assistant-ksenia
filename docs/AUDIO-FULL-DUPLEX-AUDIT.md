# Аудит AudioCaptureService, AEC и full-duplex

Обновлено: 1 сентября 2026 года.

## Вывод

Существующий Live-контур нельзя переписывать: state machine, streaming TTS, hybrid endpointing, barge-in ordering и разделение `generated_text`/`spoken_text` уже реализованы и покрыты тестами. Нижний аудиотракт теперь также имеет единый near/far PCM path и opt-in WebRTC AEC3. Незакрытая часть сузилась до измеримого качества подавления self-echo, произвольного речевого barge-in и продолжения диалога без повторной wake-фразы.

В обычном `voice-agent` теперь работает единый `AudioCaptureService`. Один дочерний процесс открывает физический input stream и публикует mono/int16 PCM по 10 мс через ограниченные очереди. `wake_worker.py`, `stt_service.py`, Vosk fallback и busy stop-monitor подключаются как потребители к одному локальному потоку:

1. Родитель запускает capture worker и получает случайный 256-битный ключ только в памяти.
2. Worker слушает только loopback; ключ передаётся дочерним потребителям через environment, не попадает в argv, repr или журнал.
3. Каждый подписчик получает только будущие 10-мс кадры; его очередь ограничена двумя секундами и при отставании отбрасывает старейшие кадры.
4. `MicrophoneCaptureGate` по-прежнему сериализует stop-monitor и голосовое подтверждение, но переключает подписчиков, не переоткрывая Bluetooth endpoint.
5. Если общий worker не запустился, старый локальный путь остаётся безопасным fallback и сохраняет прежний gate.

Это устраняет повторное открытие input внутри штатного голосового сеанса и создаёт общую монотонную сетку near-end кадров. Opt-in `PcmPlaybackController` открывает Windows output, пишет WAV точными 10-мс кадрами и публикует в capture worker только кадры, уже принятые output stream. Один аутентифицированный far publisher не может быть подменён вторым процессом; при разрыве публикации очередь очищается.

`AudioProcessor` из закреплённого `pywebrtc-audio` получает near/far одинакового размера и применяет AEC3 + NS; AGC оставлен отдельным выключенным флагом. Первый живой spike обнаружил воспроизводимое зависание far handshake не в DSP, а в блокирующем чтении Windows `stdin` pipe основным потоком capture worker. Неблокирующий `PeekNamedPipe` loop устранил дефект: три последовательных AEC-запуска после трёх секунд захвата ответили без таймаута. Строгий Silero PCM smoke затем проиграл Xenia на Tour One M3 без SAPI, а отмена длинной фразы получила worker completion через 7,2 мс.

## Статус компонентов

| Компонент | Статус | Доказательство и граница |
|---|---|---|
| `LiveSession` и streaming TTS | DONE | `src/butler/live.py`; FIFO completion и late-callback тесты |
| Generated/spoken text | DONE | только непрерывный полностью проигранный prefix попадает в память |
| LLM cancellation | DONE | живой Laguna/PoolSide gate, 500–657 мс до отмены |
| Keyword/headset barge-in | PARTIAL | stop monitor и фактический PCM stop работают; произвольная речь ещё не подключена |
| Hybrid turn detection | DONE для записываемой команды | Vosk partial + amplitude gate + semantic timing; не работает постоянно во время TTS |
| Единоличное владение устройством | DONE для `voice-agent` | один capture worker владеет endpoint; wake/STT/stop переключают подписки, fallback сохраняет прежний gate |
| `AudioCaptureService` / ring buffer | DONE для near/far transport | loopback + случайный ключ, точные 10-мс mono/int16 frames, bounded queues, один render publisher и обнаружение разрыва |
| AEC | PARTIAL | WebRTC AEC3 интегрирован; первый Tour One M3 A/B не обнаружил self-echo даже без DSP, но одновременная человеческая речь ещё не проверена |
| Noise suppression / AGC | PARTIAL | NS 0–3 подключён opt-in; AGC доступен отдельным флагом, но качество STT не принято |
| Input/output device profile | PARTIAL | input автоматически выбирается по роли и реальному open/start; PCM умеет точный Windows default или устойчивый фрагмент output, но профиль задержки устройства ещё не принят |
| Device calibration | MISSING | нет корреляционного измерения render→capture delay |

## Подтверждённая проблема выбора устройства

Последующая read-only проверка показала более опасный вариант: Windows назначила default input, но им оказался системный Stereo Mix, а рядом присутствовали line input и две Bluetooth-гарнитуры. Значит, наличие default само по себе не доказывает, что это человеческий микрофон; такой вход способен вернуть системный звук в wake/VAD.

Минимальное исправление усилено fail-closed: если selector пуст и входов несколько, Ксения просит выбрать `voice.wake_device` независимо от Windows default и ничего не открывает. Один-единственный вход по-прежнему выбирается автоматически. Ярлык списка микрофонов принимает уникальную часть имени, валидирует совпадение, затем кратко открывает endpoint тем же `open_best_input_stream` и сохраняет личную настройку только после успешного probe. Неоднозначность, отсутствие совпадения и драйверный отказ не меняют конфигурацию. Исчез также скрытый переход от несовпавшего имени к любому `Headset`/`Hands-Free`: выбранная гарнитура не может быть молча заменена другой. PortAudio index намеренно не сохраняется. Имена JBL, индексы и пути машины в код не добавлены.

Реальный START/STOP smoke 31 августа подтвердил пользовательские ярлыки и подготовку faster-whisper CUDA примерно за пять секунд, но общий capture worker и legacy wake одинаково не смогли открыть ни Tour One M3, ни Sense Pro. Оба устройства присутствовали только как WDM-KS endpoint и вернули `PaErrorCode -9999`, Windows `0x48F`; это состояние Bluetooth/драйвера, а не задержка LLM или Whisper. Личный селектор очищен, поэтому следующий запуск с несколькими входами остановится с понятным указанием на мастер, а не станет слушать Stereo Mix. После переподключения гарнитуры требуется повторить мастер и принять только успешный probe.

Живая проверка 30 августа 2026 года на подключённой Bluetooth-гарнитуре открыла default MME, явный WASAPI и WDM-KS без ошибок, но все три пути дали почти цифровой ноль (`peak 0`, `58` и `37` при пороге `220`). Новый общий worker затем отдал подписчику 200 точных кадров `320 bytes / 16 kHz` и штатно закрылся, но сигнал снова имел только `peak=5`. Следовательно, текущий отказ произошёл до Whisper и не доказывает дефект STT. До проверки индикатора входа Windows, аппаратного mute и Bluetooth multipoint менять приоритет host API или пороги нельзя.

Настоящие desktop-ярлыки запуска и остановки также пройдены. START создал валидный state, один физический capture worker, постоянный faster-whisper CUDA service и wake subscriber на одном loopback port. STOP удалил state и весь процессный подграф без оставшегося аудиосервиса.

## Подтверждённая проблема output routing и её текущее решение

Read-only инвентаризация 31 августа не открывала устройство и не останавливала голосовой сеанс. PortAudio сообщил `default_output=1`: `WCS Display`, MME, 44,1 кГц. Тот же display является default для DirectSound/WASAPI. В пользовательской конфигурации нет `voice.output_device`; текущий SoundPlayer полагается на системную multimedia routing policy.

JBL Tour One M3 виден как отдельный hands-free WDM-KS output с одним каналом и 16 кГц. Некоторые другие WDM-KS имена повреждены внутри PortAudio до символов замены, поэтому надёжно восстановить их постфактум нельзя. Индексы зависят от текущего подключения и не могут сохраняться. Следовательно, наивная замена SoundPlayer на `RawOutputStream(device=None, 48 kHz)` способна направить голос на монитор или не открыть Bluetooth-режим и не принимается.

Для render reference был принят отдельный output contract:

1. Перечислять выходы и Windows default без открытия stream.
2. Сохранять только уникальный устойчивый фрагмент имени, никогда PortAudio index.
3. При нескольких совпадениях, повреждённом имени или отсутствующем default останавливаться fail-closed.
4. При фактическом открытии сообщать resolved name, host API, channels и sample rate.
5. Не зашивать JBL, WCS, Realtek или любое другое устройство в Python/PowerShell.

`scripts/audio_output.py` реализует этот контракт для opt-in PCM. Пустой selector использует только точный текущий Windows default; явный selector выбирает уникальное имя, а индекс нигде не сохраняется. Реальный smoke разрешил маршрут `Наушники (2- JBL Tour One M3) / MME / 48000 Hz`. Legacy `System.Media.SoundPlayer` остаётся default, пока Live не принят.

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

Ограничения: проект молодой, Windows `.pyd` не имеет Authenticode-подписи. Библиотека не захватывает и не проигрывает звук; `AudioProcessor.process(near, far)` требует одинаково выровненные PCM-буферы. Wheel теперь закреплён в `runtime-assets.lock.json`, устанавливается только после проверки размера/SHA-256 и присутствует в voice venv. Это supply-chain принятие зависимости, а не доказательство акустического качества.

Артефакты исследования лежат только в исключённом из релиза `runtime/security-audit/pywebrtc-audio-0.1.0`.

## Минимальная последовательность реализации

1. **Device contract — DONE для input.** Хранится не индекс, а пользовательское предпочтение и фактически открытый endpoint; неоднозначность даёт fail-closed. В журнале есть input endpoint, sample rate, frame size и startup latency без записи звука.
2. **Единый near-end worker — DONE для `voice-agent`.** Один процесс владеет input stream и публикует 10-мс frames в ограниченные очереди. Wake, partial/final STT и stop-monitor являются потребителями. Старый путь сохранён как fallback.
3. **Output contract — DONE для opt-in PCM.** Read-only инвентаризация, точный Windows default, явный selector и фактически открытый route реализованы. SoundPlayer остаётся default до физического A/B.
4. **Render reference — DONE для transport.** PCM backend публикует только принятые output stream кадры; loopback требует случайный memory-only ключ, допускает одного publisher и очищает bounded queue при разрыве.
5. **AEC adapter — PARTIAL.** Закреплённый WebRTC wheel интегрирован, конфигурация fail-closed и реальный Xenia PCM smoke зелёный. Физический A/B 1 сентября дважды проиграл одну Xenia-фразу на Tour One M3 и не нашёл активных near-end кадров даже без AEC; DSP не включён, потому что измеримого выигрыша нет. Следующий тест должен добавить человеческую речь во время TTS, затем synthetic echo corpus при необходимости. NS и AGC сравнивать отдельно, чтобы не ухудшить окончания.
6. **Calibration profile.** Только если измерения показывают пользу: `input_endpoint`, `output_endpoint`, sample rates, estimated echo delay и AEC settings. При смене устройства профиль не переиспользовать вслепую.
7. **Physical acceptance.** Наушники, затем JBL-динамики: 20–30 ходов, паузы 1–2 секунды, произвольный barge-in, отсутствие self-interrupt, `interrupt_detected → audio_actually_stopped`, корректный spoken prefix.

`live.enabled`, `voice.playback_backend=pcm` и `live.audio_processing.enabled` остаются выключенными по умолчанию, пока пункты 5–7 не приняты на реальном устройстве. Успешное воспроизведение и установка DSP-библиотеки сами по себе не считаются готовым full-duplex.

## Физический baseline 1 сентября

`scripts/benchmark_audio_full_duplex.py` не меняет личную конфигурацию, требует тишины пользователя и хранит локальные WAV/JSON только в `runtime/audio-full-duplex/<timestamp>`. На Tour One M3 оба прохода завершились `engine=silero`, без SAPI. Без AEC near-end `measurement_rms_p95=0.53`, с AEC — `0.0`; в обоих случаях `active_frame_count=0` при пороге 80. Это доказывает отсутствие наблюдаемого self-echo в данной посадке, но не доказывает способность услышать пользователя во время TTS.

Отдельный sweep физического input block сравнил 20/40/80/250 мс. На MME 20 мс иногда дали первый callback через 656–687 мс и пачечные нулевые интервалы; 40 мс дали первый callback 31–63 мс и медианный интервал 47 мс без PortAudio status. WASAPI не улучшил долгосрочную подачу той же Bluetooth-гарнитуры, поэтому host API не переупорядочен. Default `voice.capture_block_ms` снижен с исторических 250 до измеренных 40 мс и остаётся конфигурируемым.
