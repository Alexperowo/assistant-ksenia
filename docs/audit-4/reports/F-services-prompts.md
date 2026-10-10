# Отчёт помощника F: службы и эксплуатация (A), подсказки и текст для речи (B)

Как прислан (без правок). Скрипты — `docs/audit-4/probes/agent_f/` (`filters.py`, `proposed.py`, `norm2.py`,
`selfcheck_sim.py`, `listen_sim.py`, `speak_sim.py`, `tok*.py`, `fakebin/`). Строки core.py — по `3c7b365`.
«RUN» — проверено запуском настоящего кода; «READ» — найдено чтением.

# A. Services and operations (most severe first)

**A1 — CRITICAL — If voice-out fails, Alexander gets total silence and no explanation.**
- **Where:** core/core.py:580-582 and 604-605 (`Speaker.speak`) only log the error and return. `say_notice` (1725) uses the same path. `startup_check` (2718-2723) falls back to `daily.notify`, a desktop popup. scripts/ksenia-announce:25-26 also falls back to `notify-send`.
- **Scenario:** after sleep, s2 answers 500 (CUDA error) or is down. Every reply is silent, every error phrase is silent, and the startup "не всё в порядке" message goes to a popup he cannot see.
- **RUN:** with voice-out returning 500, `speak()` returns None and sends 0 bytes of audio. Nothing else is tried.
- **Fix:**
  - Add a CPU fallback voice for `say_notice`, ksenia-announce and startup_check: `spd-say -l ru -w "…"`, or `espeak-ng -v ru`, or RHVoice.
  - In core, after 2 consecutive voice-out failures, call `control.restart_service("ksenia-voice-out")` and say through the fallback voice "Голос сломался, перезапускаю".

**A2 — CRITICAL — Nothing handles sleep/resume, and a hung GPU process is never restarted.**
- **READ:** there is no `sleep.target`, `PrepareForSleep` or `system-sleep` anywhere in the repo. All units use `Restart=on-failure`, which only reacts when a process exits. A process stuck in a CUDA call stays "active" forever. This is the past voice-in incident exactly.
- **Step 1, check first:**
  - `grep PreserveVideoMemory /proc/driver/nvidia/params`
  - `systemctl is-enabled nvidia-suspend nvidia-resume nvidia-hibernate`
- **Step 2, resume hook (concrete).** As root, `/etc/systemd/system/ksenia-sleep.service`, then `systemctl enable ksenia-sleep`:
  ```
  [Unit]
  Description=Ksenia: free GPUs before sleep, bring her back after resume
  Before=sleep.target
  StopWhenUnneeded=yes
  [Service]
  Type=oneshot
  RemainAfterExit=yes
  ExecStart=-/usr/bin/systemctl --user -M user@ stop ksenia-judge ksenia-voice-in ksenia-voice-out ksenia-brain
  ExecStop=-/usr/bin/systemctl --user -M user@ start --no-block ksenia-resume.service
  [Install]
  WantedBy=sleep.target
  ```
  - Core does not need stopping: it holds no GPU and owns the headset buttons.
  - Trade-off of stopping the brain: model reload plus ~13 s history recompute after resume. Keeping the brain running only works if the NVIDIA preserve setting from step 1 is on.
  - User unit `ksenia-resume.service`: `Type=oneshot`, `TimeoutStartSec=6min`, `ExecStart=%h/Agents/Ksenia/scripts/ksenia-resume`. The script:
    1. Runs `timeout -k 5 20 nvidia-smi -L`. If that fails, the driver is hung: say through `spd-say -l ru -w` "Видеокарта не проснулась после сна, нужна перезагрузка", show a notification, exit 1.
    2. Otherwise runs `systemctl --user start ksenia-voice-out ksenia-voice-in ksenia-judge ksenia-brain`.
    3. Polls for up to 5 min: `curl -sf :18100/health`, `curl -s -o /dev/null :18110/`, and `curl -sf :18120/headset`. Use `/headset`, not `/status`, which blocks the event loop (see A3).
    4. If anything is still down, speak about it through the fallback voice.
  - User-only alternative without root: a `gdbus monitor --system --dest org.freedesktop.login1` listener on `PrepareForSleep(false)`. Stopping services before sleep this way needs `systemd-inhibit --mode=delay`, which logind limits to 5 s by default (`InhibitDelayMaxSec`).
- **Step 3, health watchdog:** `ksenia-health.timer` (`OnBootSec=4min`, `OnUnitActiveSec=1min`) running a oneshot script that:
  - skips units started less than 180 s ago (`ActiveEnterTimestampMonotonic`);
  - probes brain `/health`, judge `/health`, voice-out `/` (503 counts as alive), voice-in `/headset`, core `/status` and `timeout -k 2 10 nvidia-smi`, each with a 5 s timeout;
  - after 3 consecutive failures runs `systemctl --user restart <unit>` and announces it (through core `/notice` if voice works, otherwise `spd-say`).
  - For the Python services also add `Type=notify` + `WatchdogSec=30` with an sd_notify ping from the event loop. This catches a stalled event loop but not a stuck worker thread.

**A3 — HIGH — A hung voice-in makes Ksenia say "Я ещё дослушиваю прошлую фразу" forever.**
- **Where:** voice-in/voice_in.py:560-561 answers 409 while `ear.lock` is held; 601 runs `transcribe_sure` in a thread. In core: `listen` 1861-1876 retries 409 for 20 s; then 1893 + 1822 speak the "busy" phrase.
- **Scenario:** after resume, a CUDA call in that thread never returns, so the lock is held forever.
- **RUN:** with voice-in permanently answering 409, three conversations in a row each said "[sigh] Я ещё дослушиваю прошлую фразу. Нажми ещё раз через пару секунд."
- **Fix:**
  - In voice-in: wrap the recognition call in `asyncio.wait_for(..., 20)`; on timeout log and `os._exit(1)` so systemd restarts it. Expose how long the lock has been held in `/status`.
  - In core: after 2 "busy" results with nothing being recorded, restart voice-in and say "Слух завис, перезапускаю, секунд двадцать".

**A4 — HIGH — The self-check reports "all fine" when the GPU driver and voice-in are hung.**
- **Where:** core/tools/selfcheck.py
  - 75-84: an nvidia-smi error or timeout is silently ignored.
  - 46-49: voice-out and voice-in are only checked with `is-active`.
  - 62-71: a voice-in timeout is ignored.
  - 14-15: ksenia-pwa and ksenia-unblock are missing (control.py:48-50 has them).
  - 27-33: on timeout the child process is never killed, so hung nvidia-smi processes pile up.
- **RUN:** all services "active", voice-in curl timing out, nvidia-smi timing out → `problems: []`.
- **Fix:** if nvidia-smi fails or returns no rows, report "видеокарта не отвечает"; probe voice-out and voice-in over HTTP; add pwa and unblock; `p.kill()` on timeout.

**A5 — HIGH — Switching back to "ksenia" never restarts the tablet gateway.**
- **Where:** scripts/ksenia-mode:10 ("nexus") stops `ksenia-pwa`; line 16 ("ksenia") starts only voice-out, voice-in, brain, judge and core.
- **RUN** (stubbed systemctl): the start line has no `ksenia-pwa`.
- **Effect:** the tablet and the "Ксения — управление" desktop shortcut (it opens :18142, served by ksenia-pwa) stay dead until reboot.
- ksenia-unblock is never stopped, so nothing else needs restoring.
- **Fix:** one list variable used by both branches, including ksenia-pwa. The unused `KSENIA` variable on line 5 shows how the two lists drifted apart.

**A6 — MEDIUM — ksenia-mode says "Я снова с тобой" even when nothing came up.**
- **Where:** scripts/ksenia-mode:17-21.
- **RUN:** brain never healthy → 90 health checks (about 3–6 min) → still proceeds to line 21, "Я снова с тобой".
- It never waits for core (:18130) or voice-in, so the first tap right after the phrase can go nowhere. There is no lock against a double-click on the shortcut. Line 19's `; [ $? -eq 0 ]` is fragile.
- **Fix:** `if curl -sf … && curl -s … :18110/ && curl -sf … :18130/status; then ok=1; break; fi`. After the loop, say a failure phrase when `ok` is not set. Add `exec 9>"$XDG_RUNTIME_DIR/ksenia-mode.lock"; flock -n 9 || exit 0`.

**A7 — MEDIUM — When core is down, headset taps do nothing at all.**
- **READ:** the headset buttons are handled by a helper that lives inside core (core.py:2471 `HeadsetButtons`). The Nexus drop-in (services/nexus-dropins/ksenia-gpu.conf:3) stops ksenia-core via `Conflicts=`. The only way back is a desktop icon, and he is rarely at the computer.
- **Fix:**
  - `ExecStopPost=-…/ksenia-crash-note %n` on the units. The script checks `$SERVICE_RESULT` and, if it is not `success`, speaks through the fallback voice (rate-limited).
  - In Nexus mode, a tiny CPU-only standby headset-button listener that says "Сейчас работает Антигравити; коснись дважды, чтобы вернуть меня" and runs `ksenia-mode ksenia`.

**A8 — MEDIUM — Start order gives no readiness, and the error phrases ask a blind user to check services.**
- **READ:** all units are `Type=simple`, so `After=` (core.service:3, pwa.service:3) only orders process start, not readiness.
- A tap during the brain's 503 "Loading model" phase speaks `BRAIN_FAIL_PHRASE` (core.py:835, used at 1327): "Мозг не отвечает, проверь, пожалуйста, сервис". `LISTEN_DOWN` (1824) also says "Проверь, пожалуйста, сервис" — he cannot do that.
- pwa's `After=network-online.target` does nothing in the user manager (that is a system unit), and there is no `Wants=`.
- **Fix:** treat a 503 from the brain as "Я ещё просыпаюсь, подожди полминуты" and retry. Replace "проверь сервис" with an automatic restart plus a time estimate.

**A9 — MEDIUM — A hung brain means one "Хм, секунду" and then up to 3 minutes of silence.**
- **READ:** core.py:1263 uses `ClientTimeout(total=180)`; `_filler` (1217) speaks only once.
- **Fix:** add `sock_read=25` (time to first token), a second spoken notice, and the restart path from A2/A8.

**A10 — MEDIUM — The startup check can be skipped for the whole boot and never runs after resume.**
- **READ:** core.py:2705 writes the once-per-boot mark *before* the 240 s check. If core restarts during boot, the check is skipped until the next reboot. Resume keeps the same boot id, so it never runs after sleep.
- **Fix:** write the mark after the check finishes; trigger the check from ksenia-resume.

**A11 — MEDIUM (unverified) — Core may start without the Wayland display variables.**
- **READ:** all units are `WantedBy=default.target`. Core may start before Plasma imports `WAYLAND_DISPLAY` into the user manager. spectacle, wl-paste and kscreen-doctor (core/tools/screen.py:96, 207) would then fail until core restarts.
- **Fix:** core.service `After=graphical-session.target`, `PartOf=graphical-session.target`, `[Install] WantedBy=graphical-session.target`.
- **Verify after a cold boot:** `tr '\0' '\n' </proc/$(pgrep -f core.py)/environ | grep WAYLAND`.

**A12 — MEDIUM — Logs grow forever.**
- **READ:** every unit uses `StandardOutput=append:` with no rotation.
- voice-out/run_voice_out.sh:7 leaves `S2_CODEC_PROF=1` on. In the fork that prints one `[CodecProf]` line per partial decode (src/s2_codec.cpp:1350, 1393-1397). s2 also logs [START]/[END] per request.
- weekly_report.py:20 reads the whole core.log every week. It also ignores restarts and the voice-in/voice-out/brain logs.
- **Fix:**
  - A user timer running `logrotate -s ~/.local/state/ksenia-logrotate.status` on `logs/*.log` with `size 20M, rotate 5, compress, copytruncate, missingok` (copytruncate works with `append:` because the file is opened with O_APPEND).
  - Remove `S2_CODEC_PROF`.
  - Have the weekly report also read `core.log.1` and the restart count (`NRestarts`).

**A13 — MEDIUM (read, not run here) — After the planned clean reinstall every service fails.**
- `logs/` is gitignored and nothing creates it. systemd does not create parent directories for `append:`, so every unit fails with 209/STDOUT and restart-loops — a silent Ksenia.
- **Fix:** add `logs/.gitkeep` with gitignore lines `logs/*` and `!logs/.gitkeep`, or create the directory in an install step.

**A14 — LOW — The brain API key is visible in `ps`.**
- scripts/ksenia-mode:18 puts the key on the curl command line during each check (up to 90). `/health` is public in llama-server and does not need the key.
- **Fix:** drop the header, or use `-H @file`.

**A15 — LOW — Restart policy.**
- voice-in exits with code 0 on any SIGTERM (voice_in.py:913-918). With `on-failure`, an external kill is never restarted.
- **Fix:** use `Restart=always` for the daemons; `systemctl stop` and `Conflicts=` still stop them.
- No change needed for the start limit: with RestartSec ≥ 5 s there are at most 3 starts per 10 s, below the default limit of 5.

**A16 — LOW — Hardcoded `/home/user` paths in 11 runtime files.**
- Units mix `/home/user` with `%h`. run_voice_in.sh:5 hardcodes the `python3.12` site-packages path: after a Python upgrade the CUDA libraries are not found and recognition silently falls back to CPU.
- The voice-in service description and the run_voice_in.sh header still say "Whisper"; the engine is GigaAM. run_brain_nex.sh:8-9 uses cuda-13.1.

**A17 — LOW — The judge's GPU placement is implicit.**
- brain/run_judge.sh:7-8 uses `--device CUDA1` without `CUDA_DEVICE_ORDER=PCI_BUS_ID` (every other script sets it), so it depends on CUDA's fastest-first ordering. `--log-disable` leaves no diagnostics.
- **Fix:** set `PCI_BUS_ID`, `CUDA_VISIBLE_DEVICES=1` and `--device CUDA0`.

**A18 — LOW — ksenia-announce gives up on "busy".**
- It has no retry on 503 busy; the fork returns 503 while it is synthesizing (src/s2_server.cpp:715-717). If Ksenia is talking, "Наушники переподключила" goes to a popup.
- **Fix:** retry for 15 s, like core does.

**A19 — LOW — Every notification is titled "Напоминание".**
- core/tools/daily.py:107-112 uses that title and an appointment icon for every notification, including startup problems and VK messages.

---

# B. Prompts and speech text

**B1 — HIGH — Stories are cut off mid-sentence.**
- **Where:** `max_tokens` is 400 (core/config.json:14; core.py:1239 adds a budget of 0 for chat). There is no `finish_reason` handling anywhere.
- **Contradicts:** persona.md:12-13 ("8–15 предложений"), 71-73 ("не меньше трёх абзацев") and 100 (fairy tale "2–4 минуты").
- **Measured:** Russian is 2.4–2.8 tokens/word (Qwen BPE), so 400 tokens ≈ 150–165 words ≈ 60–75 s of speech. The tokenizer used is Qwen1's `qwen.tiktoken`, not Bonsai's own — counts may be off by roughly 5–10%.
- **Fix:** raise `max_tokens` to 1200–1500 when the request is a story or fairy tale, or automatically continue when `finish_reason=="length"`.

**B2 — HIGH — English number words inside band and song titles are turned into digits.**
- **Where:** core.py:217-230 `fix_english_numbers`.
- **RUN:**

| Input | Output |
|---|---|
| Twenty One Pilots | 21 Pilots |
| Nine Inch Nails | 9 Inch Nails |
| Metallica — One | Metallica — 1 |
| Seven Nation Army | 7 Nation Army |
| Take Five | Take 5 |
| Thirty Seconds to Mars | 30 Seconds to Mars |
| one hundred | 1 hundred |

- **Fix (tested):** inside the replacement, leave the match alone if the nearest word on the left or right is Latin:
  ```python
  left = re.search(r"([A-Za-zА-Яа-яЁё]+)[^A-Za-zА-Яа-яЁё]*$", text[:m.start()])
  right = re.match(r"[^A-Za-zА-Яа-яЁё]*([A-Za-zА-Яа-яЁё]+)", text[m.end():])
  if (left and re.match("[A-Za-z]", left[1])) or (right and re.match("[A-Za-z]", right[1])): return m[0]
  ```
  - All titles above stay unchanged. The existing test "плюс thirteen, от plus four до plus eleven" still gives "плюс 13, от плюс 4 до плюс 11".
  - Remaining false positive: "альбом Ten" → "альбом 10".

**B3 — HIGH — `feminine()` breaks correct sentences and misses some errors.**
- **Where:** core.py:235-264.
- **RUN, wrong outputs:**

| Input | Output | Problem |
|---|---|---|
| Я тоже футбол люблю | Я тоже футбола люблю | noun after "я" (line 256 rule) |
| Я сериал досмотрела | Я сериала досмотрела | same (also канал, сигнал, материал) |
| Я один раз там была | Я одна раз там была | "один раз" means "once" |
| Ты говоришь «я устал» | «я устала» | Alexander's quoted words changed |
| Он сказал: я сам видел | я сама видел | half-fixed, mixed gender |

- **Misses:** "Я ошибся", "Я заблудился", "Я, честно говоря, не понял", "Я тебе так и не сказал".
- **Fix (tested; passes all 17 existing cases in tests/test_humanity.py):**
  - skip text inside «…» or "…";
  - skip "один" when "раз" follows;
  - add a noun stoplist (футбол, сериал, канал, сигнал, материал, финал, журнал, зал, стол, пол, …);
  - skip the generic "-л" rule when the next word is a feminine past verb (`\s+[а-яё]+(?:ла|лась)\b`);
  - turn reflexive `(\w+?[аяеиыуо])лс[яь]` into `…лась`, and "ошибся" into "ошиблась".

**B4 — MEDIUM — The emotion-tag limiter suppresses too much at the start of replies and nothing elsewhere.**
- **Where:** `tag_too_often` (core.py:1347-1350) counts the model's raw tags, including ones it already stripped (1209-1210).
- **RUN (simulation of 9 replies):** once the model tags every reply, the next 3 tagged replies ([teasing], [laughing], [warm]) were stripped, plus [sigh] after one untagged reply. Tags came back only after two untagged replies in a row.
- The check only looks at the first tag of the first step (1300-1303). `clean_for_speech` (283) keeps every allowed tag in the middle of a reply, after a tool call, and several per reply. RUN: "Ну конечно. [teasing] Опять ты за своё." is spoken with the tag.
- **Three different rules:** persona.md:23 — about 1 reply in 4; persona.md:24 — [teasing] once per conversation; core — 1 in 3.
- **Prompt push towards teasing:** persona.md:4, persona.md:94 and self.md:3 ("любишь подначить") all encourage it.
- **History reinforces it:** the history keeps raw tags (it must, for the brain cache), so the model keeps seeing its own [teasing] and copies it.
- **Fix:** apply one policy at voice level: at most 1 tag per reply; record the tag that was actually *spoken*; [teasing] once per conversation (reset in `Conversation.run`). Optional: per-request `logit_bias` against the token "asing" ("[teasing]" splits into `[`, `te`, `asing`, `]`) after teasing has been used. Verify on Bonsai's tokenizer first.

**B5 — MEDIUM — speech_norm.py mistakes.**
- **Wrong stress on "начала":** line 58 puts the verb stress on the noun. RUN: "С самого начала́", "До начала́ фильма". Fix: remove `начала'`.
- **Year rule eats grams:** line 84. RUN: "5 г. сахара" → "5 года сахара". Fix: require 3–4 digits, or a 1–4 digit number before "до н. э.".
- **Degree sign left in ranges:** line 86. RUN: "12-14°" and "+12…+14°" keep the "°". Fix: add a second pass `NUM\s?°\s?[CС]?(?!\w)` → plural of "градус".
- **Sign rules too narrow:** lines 96-97. RUN: "–5" (en dash) and "«+14»" are not converted. Fix: use the lookbehind `(?<=[\s(…«])|^` and add "–" to the minus rule.
- **Wrong case:** "с 2 мм" → "с 2 миллиметра" (should be genitive). Low.
- **Patched copy RUN:** "минус 5", "плюс 12…плюс 14 градусов", "12-14 градусов", "5 г. сахара" unchanged, "С самого начала" without the stress mark.

**B6 — MEDIUM — The fixed prompt is about 10.7k tokens.**

| Part | Characters | Tokens |
|---|---|---|
| persona.md | 10,452 | 3,823 |
| self.md | 1,317 | 496 |
| tool schemas (JSON) | 20,564 | 6,417 |

- Of the tool tokens, 3,268 are descriptions; the `system` tool alone is 701.
- That is ~10.7k tokens before memory, diary and history. At the measured 490 tok/s it is ~22 s on a full cache miss; the observed ~13 s suggests faster throughput on long prompts.
- **Cheap cuts:** persona.md:35 is a tool inventory that duplicates the tool schemas (444 tokens); the games block 93-102 (399 tokens) could be returned by a tool only when a game is requested; duplicated rules: 37-38 and 50; 43-44, 66 and 88; 52-55 and 59.
- Saving about 1.2–1.5k tokens is worth ~1.5–3 s per cache miss. Any persona edit invalidates `brain_prefix.json` (it stores a hash of the persona), costing one full recompute.

**B7 — MEDIUM — Contradiction: proactive news versus "don't invent facts".**
- self.md:5-7 ("можешь сама о них заговорить") and core.py:1104 ("можешь сама предложить свежую новость") versus persona.md:31-33 (never invent events) and 81-82 (don't search the internet unless asked). persona.md:56-58 also says "СРАЗУ ответь тем, что знаешь сама" about fresh events.
- **Risk:** the 2-bit model invents model-release "news".
- **Fix:** "о свежих новостях — только из research_background; предложи, но не рассказывай по памяти".

**B8 — MEDIUM-LOW — The model is told both to ask and not to ask confirmation questions.**
- persona.md:53 says core speaks the confirmation question word for word and the model must not repeat it (`speak_verbatim`).
- persona.md:59 says "спроси «Нажать …?»"; persona.md:64 says "можешь спросить «Запомнить?»" while the memory tool asks too. Result: the question is asked twice.
- **Fix:** delete the second half of line 59 and reword line 64.

**B9 — MEDIUM-LOW — Question rules contradict each other and the filter.**
- persona.md:15 bans "Продолжить?" while persona.md:45 prescribes "Дальше читать?".
- `OFFER_RE` (core.py:816-818) includes "как тебе", "что скажешь" and "интересно" — exactly the opinion questions persona.md:14-15 allows. RUN: "Как тебе такая идея?" is cut as an offer when `offer_too_often` is true. RUN: it misses "Продолжим?" and "Ещё?".

**B10 — LOW-MEDIUM — Guess or ask when speech is unclear?**
- persona.md:84-88 says "догадывайся… не переспрашивая"; the per-turn note at core.py:1081 says "не угадывай, переспроси".
- **Fix:** have the persona defer to the service note.

**B11 — LOW — Rules a 2-bit model may take too literally.**
- persona.md:12-13 makes a bare "расскажи" trigger 8–15 sentences, so "расскажи, какая погода" can become a lecture. Limit it to "историю/сказку/подробнее".
- persona.md:81-83 ("скажи, что пока не умеешь; не пробуй обходные действия") conflicts with the ROADMAP principle "Списки — примеры, а не рамки".
- persona.md:90 hardcodes night as 23–7, but the night hours are user settings (control.py) and core already lowers the volume (night_gain).
- persona.md:28 calls the service note "дата и время", but it now carries instructions.
- persona.md:100 has a typo ("тёпло") and a family detail; the audit task's own rules say family details belong only in data/memory.json.

**B12 — LOW — self.md versus persona.md.**
- persona.md:1 calls her "помощница"; self.md:3 says "не послушный помощник".
- The GPU joke in self.md:8-9 will repeat across conversations; persona.md:80 only prevents repeats within one conversation.

**What a deterministic filter can fix, and what it cannot (regexes tested):**
- **Safe:**
  - "обо":
    ```
    \b([Оо])бо(\s+)(?!(?:мне|всём|всем|всех|всё|все|что|чём|льду)\b)(?=([А-Яа-яЁё]))
    ```
    Replace with "об" before а/о/у/э/и/ы, otherwise "о". RUN: "обо тебе"→"о тебе", "обо этом"→"об этом", "обо Маше"→"о Маше", "обо ёлке"→"о ёлке"; "обо мне", "обо всём" and "обо что-то" unchanged.
  - "сам" with no "я" before it:
    ```
    (?<!\bты\s)(?<!\bон\s)(?<!\bвы\s)\b([Сс])ам(\s+(?:не\s+)?(?!(?:всю|ту|эту|мою|твою|свою|нашу|вашу|одну|себя|себе)\b)[а-яё]+[ую])\b
    ```
    Replace with "Сама/сама". RUN: "Сам могу"→"Сама могу", "сам не знаю"→"сама не знаю"; "Ты сам видишь" and "сам по себе" unchanged. The lookbehinds are case-sensitive, so a sentence-initial "Ты сам …" needs a "Ты" variant too.
  - The B2, B3 and B5 fixes above.
- **Exact-word map only:** misforms like "договарю"→"договорю". pymorphy3 `word_is_known("договарю")` is False, so a weekly scan of logged replies can collect new ones. Never auto-correct general unknown words.
- **Not safe generically:** "для Машу и Насти". With pymorphy3, "Машу" also parses as the verb "машу", so a generic re-inflection leaves it unchanged. Only a closed list of names from data/memory.json after для/у/от/без/из/до/кроме would be safe. Everything else (verb conjugation, case agreement) needs few-shot correct examples in the prompt rather than rules, or a higher-bit model.

---

# Not tested (no systemd, GPU, Fish S2 or the real machine here)
- The systemd behaviour relied on: `append:` not creating parent directories, `network-online.target` in user units, the `StopWhenUnneeded`/`sleep.target` pattern, `systemctl --user -M user@`.
- Whether NVIDIA preserve-video-memory and the suspend services are enabled.
- Whether s2 and onnxruntime hang or return errors after resume.
- How Fish S2 reads "10:30", "1-й", "0.4", "т. е.", "°", and the U+0301 stress marks.
- The display environment of core after a cold boot.
- The `ksenia` CLI behind Ctrl+Alt+K (not in the repo) when core is down.
- headset.py needs a sudoers rule for `sudo -n systemctl restart bluetooth`; it is not in the repo.
- The `logit_bias` token id on Bonsai's tokenizer.
- Token counts exclude the memory and diary blocks.
