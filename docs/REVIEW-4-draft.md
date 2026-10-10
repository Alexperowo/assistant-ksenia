# Сводка находок агентов (для REVIEW-4), статус: [F]=исправлено, [R]=в отчёт, [ ]=решить

## Мои
- M1 [F] системная подсказка пересобиралась после паузы (дневник) + перезапуск ядра → пересчёт ~13 с (4c58513)
- M2 [F] is_affirmative: «давай потом», «да ну», «Да? А кому?» (4b48de0)
- M3 [F] песочница: решала подтверждение, TEST_SINK глобальный (30a6a72)
- M4 [F] create_task без ссылки (3c7b365)
- M5 [ ] ksenia-mode ksenia не поднимает ksenia-pwa; «Я снова с тобой» даже если мозг не поднялся

## C (system/settings/net)
C1 HIGH подтверждённое действие режется 60 с (install 900, update 3600, wifi ~200 с); _exec/_run не убивают процесс при отмене
C2 HIGH apt: «pipewire-» = удалить; PROTECTED точные имена; -y удаляет зависимости; clean autoremove без симуляции
C3 HIGH оплата: web.py 23-25,165-168 нужен и финансовый URL, и слово; «Купить в 1 клик», «Продолжить» на checkout, голоса ВК
C4 MED net_guard keep-alive: проверяется только первый запрос на соединении (net_guard.py:100-110,125)
C5 MED _run_change без "error" → «НЕ удалось: None» (system.py:167)
C6 MED sudo -n env ... apt-get: sudoers на env = root на всё (system.py:27-28) → обёртка ksenia-pkg [R]
C7 MED dpkg conffile prompt, full-upgrade удаляет (system.py:154-158)
C8 MED подтверждённый web-клик ищется заново по тексту (web.py:256-260,159-170)
C9 MED подтверждение переживает служебные ходы (= A1#1, B#1)
C10 MED mute / volume_set 0 глушат саму Ксению (settings.py:167-175)
C11 MED YouTube video=true не показывает видео (music.py:279 инвертировано)
C12 MED Wi-Fi: «unknown» → откат рабочей сети; откат по SSID; «:» в SSID; профиль не удаляется (settings.py:124-153)
C13 MED до 60 с тишины после «да» (filler стартует позже)
C14 LOW погода: 429/5xx геокодера = «город не найден» (daily.py:145-153)
C15 LOW сырые исключения в речь (headphones, web_open, bing ParseError, yt-dlp, settings._run FileNotFoundError, selfcheck repr)
C16 LOW selfcheck._run не убивает процесс (selfcheck.py:30-33)
C17 LOW WebRTC UDP мимо net_guard (browser_core.py:43-45)
C18 LOW IPv6: 64:ff9b::7f00:1, ::7f00:1, ::ffff:0:7f00:1, fec0::1 считаются публичными (net_guard.py:27-31)
C19 LOW свой публичный IP (NAT loopback) [R]
C20 LOW адреса радио не проверяются (music.py:178-191) [R]
C21 LOW unblock CONNECT куда угодно (127.0.0.1:18100 и т.п.), только 127.0.0.1 (unblock.py:57-72)
C22 LOW notify-send без -- (daily.py:109-110)
C23 LOW пароль Wi-Fi в журнале/истории/argv
C24 LOW search_package с ведущим «-» (system.py:72)

## B (confirmations/tools)
B1 CRIT ожидающее действие переживает служебный ход: «ага» на напоминание отправляет ВК (=A1#1)
B2 HIGH вопрос не прозвучал (перебили/TTS сбой), действие ждёт; «давай быстрее» отправляет
B3 HIGH оплата web+desktop (desktop.py:22-23,298,377)
B4 HIGH memory_remember без вопроса (affirmative / «запомн» в вопросе) (memory.py:74-77)
B5 HIGH watch_rule add: длинный who → память/подсказка (watch.py:64-71)
B6 HIGH web_type + web_click «Отправить» на vk: вопрос без текста; «Опубликовать» без вопроса
B7 HIGH dictate(enter): вопрос text[:80]; фокус не закреплён; терминал (desktop.py:406-408,339-359)
B8 HIGH is_affirmative всё ещё широк: «отправь Маше», «давай заново», «да включи музыку», «ок, понятно»
B9 MH неизвестный голос (owner None, enrolled) = Александр; планшет не передаёт speaker (gateway 486-503)
B10 MH два риск-действия в ходе — одно «да» запускает последнее; same label new text → старый текст (confirm.py:35,45)
B11 MH voice_enroll clear / voice_mode guest без подтверждения (voicectl.py:59-79)
B12 MED отмена во время подтверждённого действия — полусделано, не записано (=A1#13)
B13 MED memory_forget по одному без вопроса; remind_cancel("а") удаляет все
B14 MED напоминание теряется при перебивании (daily.py:97-104, deliver_waiting pop) (=A1#4)
B15 MED screen_click: риск по слову модели, «Оп» → «Оплатить»; старые координаты (desktop.py:272,298-299)
B16 MED desktop risky list: Установить/Install, Да/OK, Не сохранять, Отключить/Забыть, Выполнить (desktop.py:22-23)
B17 MED ui_type заменяет весь текст документа (atspi_helper.py:202-203)
B18 MED web_type в любое поле; Enter без вопроса на «поиске» (web.py:262-279,131-132)
B19 MED web_open — утечка данных в query [R]
B20 MED живой режим: «да» во время речи/думания = continue, теряется; истечение молча (=A1#12)
B21 MED чужой текст помечен «данные» не везде (screen.py:154-159) + «заражённый» ход
B22 LM xdg-open исполняемых (files.py:199-201)
B23 LM ВК: ссылки «ссылка» в вопросе; одинаковые имена → found[0] (vk.py:173-176)
B24 LM research без общего таймаута; «сломался: {e!r}» (research.py:53,71-111)
B25 LOW технические ошибки в речь; ВК при таймауте «НЕ удалось», может уйти дважды
B-small: voicectl._save_mode не атомарно; memory._load не-список → затрёт; files._move минутное имя коллизия; «окн» гейт = «что в окне?»; песочница не сообщает планшету о вернувшемся вопросе

## A1 (core turn/history)
A1-1 HIGH «да» на другой вопрос (=B1)
A1-2 HIGH is_affirmative (часть исправлена)
A1-3 HIGH 60 с (=C1) + тишина
A1-4 HIGH напоминание теряется (=B14); live_waiting стартует посреди его фразы; склейка со старой репликой
A1-5 HIGH мозг/голос упал — текст напоминания не сказан (BRAIN_FAIL only)
A1-6 HIGH(live) перебивание: история короче сгенерированного → пересчёт после каждого перебивания? проверить вживую (brain.log)
A1-7 [F] M1
A1-8 MH окно прыгает каждые ~11 ходов (history_max 60) → поднять, прыжок в простое, прогрев n_predict=0
A1-9 MH песочница: (b) confirm.ask в инструментах, (c) NEW_TOOLS/interrupted note, (d) стоп в песочнице, (e) побочные инструменты → allow-list
A1-10 MED /say sandbox: ks.stop, duck, TEST_SINK (частично F)
A1-11 MED memory без подтверждения (=B4)
A1-12 MED live «да» во время busy (=B20)
A1-13 MED отмена во время действия (=B12)
A1-14 MED weak voice + expired → AttributeError (core.py:993); hello съедает истечение; live_turn без except
A1-15 MED voice-out завис — нет запасного звука, держит lock до 10 мин; cancel не прерывает POST
A1-16 MED мозг завис — 180 с тишины
A1-17 MED live: слух закрыл поток — молча
A1-18 MED напоминания на планшет во время разговора в наушниках (preferred_output)
A1-19 MED fix_english_numbers: One/Seven Nation Army/Take Five/«one two three four»
A1-20 MED feminine: «канал переключу»→«канала», цитаты чужой речи
A1-21 LM speech_norm: «начала» ударение, «200 г. муки», «1 градус», «2 -1»
A1-22 LM split_first_sentence режет «рт. ст.»
A1-23 LM повтор пустого шага — пересчёт
A1-24 LM дневник: ошибка мозга = upto сдвинут; гонка upto; индекс после обрезки 200; «сегодня/вчера» в подсказке

## F (services/ops + prompts)
FA1 CRIT голос не работает → полная тишина, ошибки тоже молча; startup_check в уведомление → запасной голос spd-say/espeak-ng/RHVoice; рестарт voice-out после 2 сбоев
FA2 CRIT нет сна/пробуждения; зависший GPU-процесс не перезапускается (Restart=on-failure) → ksenia-sleep (system), ksenia-resume (user), ksenia-health.timer, WatchdogSec [R+скрипты?]
FA3 HIGH завис voice-in → «Я ещё дослушиваю» вечно (409) → wait_for 20 c + os._exit в слухе; ядро после 2 «busy» перезапускает слух
FA4 HIGH самопроверка «всё в порядке» при зависшей видеокарте/слухе (selfcheck.py 75-84,46-49,62-71,14-15,27-33)
FA5 HIGH ksenia-mode ksenia не поднимает ksenia-pwa (=M5)
FA6 MED «Я снова с тобой» даже если не поднялось; нет блокировки двойного запуска
FA7 MED ядро выключено — касания ничего [R]
FA8 MED Type=simple, After= без готовности; «проверь сервис» слепому; 503 Loading model → «просыпаюсь»
FA9 MED мозг завис 180 с (=A1-16)
FA10 MED метка самопроверки пишется до проверки; после сна не запускается
FA11 MED WAYLAND_DISPLAY: core до графического сеанса [R/проверить]
FA12 MED логи растут; S2_CODEC_PROF=1; weekly_report читает весь лог
FA13 MED logs/ нет в git → append: падает 209 после переустановки → logs/.gitkeep
FA14 LOW ключ мозга в ps (ksenia-mode curl -H)
FA15 LOW voice-in выходит 0 на SIGTERM; Restart=always
FA16 LOW /home/user, python3.12 в run_voice_in.sh, «Whisper» в описании
FA17 LOW судья --device CUDA1 без PCI_BUS_ID
FA18 LOW ksenia-announce без повтора на 503
FA19 LOW все уведомления «Напоминание»
FB1 HIGH max_tokens 400 режет рассказы (persona: 8–15 предложений, сказка 2–4 мин); finish_reason не смотрим
FB2 HIGH fix_english_numbers: Twenty One Pilots, Nine Inch Nails, One, Take Five (=A1-19)
FB3 HIGH feminine: «футбол люблю»→«футбола», «один раз»→«одна раз», цитаты; пропуски «ошибся» (=A1-20)
FB4 MED тег-ограничитель: считает сырые теги; середина ответа не ограничена; 3 разных правила; persona подталкивает к teasing
FB5 MED speech_norm: «начала́», «5 г. сахара», «12-14°», «–5», ««+14»» (=A1-21)
FB6 MED подсказка ~10.7k токенов; persona:35 инвентарь дублирует схемы; игры 93-102; дубли
FB7 MED новости про нейросети «сама» vs «не выдумывай» → только через research_background
FB8 ML вопрос подтверждения дважды (persona 59, 64)
FB9 ML «Продолжить?» запрещено vs «Дальше читать?»; OFFER_RE режет «как тебе» (разрешено)
FB10 LOW угадывай vs переспроси
FB11 LOW «расскажи» = 8–15 предложений; «не пробуй обходные» vs «списки — примеры»; ночь 23–7 захардкожена; опечатка «тёпло»; дочки в persona
FB12 LOW «помощница» vs «не послушный помощник»; шутка про видеокарты повторяется
Фильтры: «обо тебе»→«о тебе» (regex), «Сам могу»→«Сама могу» (regex), словарь «договарю»; «для Машу» — нельзя

## D (слух/голос)
D1 HIGH сбой/зависание CUDA (после сна) не лечится: lock держится → 409 вечно; init без CPU-запаса → wait_for 10 c + os._exit(1); CPU-запас; nvidia-smi в run_voice_in.sh; WatchdogSec
D2 HIGH короткое «да» (<0.6 с) при записанном образце = owner None → проходит (core weak_voice) — ИСПРАВЛЯЕТСЯ в WIP
D3 HIGH(guest) шаговый режим: отпечаток по тишине (record_utterance с начала parec) → обрезать до t_speech-0.3
D4 MH pacat сигнала без таймаута (voice_in.py:293-300,739-746) → lock навсегда
D5 MED исключение в живом цикле → молчаливый CLOSE; ядро молчит
D6 MED живой режим: реплика не кончается при ровном фоне (max_s только на тишине; порог 0.0056)
D7 MED max_s 120 > таймаут /listen ядра 90 с
D8 MED «висящее слово» раньше вопроса: «Что?», «Давай.» ждут 3 с
D9 MED после «стоп» 0,5 с полной громкости (буфер), затухание не слышно; двойное усиление; невыровненные байты
D10 MED наушники остаются в HFP после убийства /listen
D11 MED ответ TTS после «стоп» запускает новый плеер — щелчок
D12 MED голос упал — тишина (=FA1)
D13 MED запись образца/owner_only: может запереть Александра; «стоп» игнорируется в owner_only при ложном отказе
D14 LM восстановление наушников минутами, без паузы между попытками
D15 LOW handle_status pactl синхронно
D16 LOW гонка двух /stream
D17 LOW find_mac кэширует первое «Audio Sink»
D18 LOW логи; Speaker.recorded в памяти; save_recording синхронно; Mood._save не атомарно
D19 LOW _apply_gain без clip при gain>1
D20 LOW turn_replay не находит конец
D21 LOW задачи без ссылок в voice_in.py:799, headset.py:245; p.wait после kill

## Не успели: A2 (разговор/живой режим/центр), E (планшет), G (скорость) — остановлены по лимиту.
## Состояние ветки: см. git log; последний коммит — WIP переделки подтверждений (см. сообщение коммита).
