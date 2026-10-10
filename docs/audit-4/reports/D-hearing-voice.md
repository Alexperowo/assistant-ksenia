# Отчёт помощника D: слух (voice-in), отпечаток голоса, наушники, озвучка (Speaker), службы

Как прислан (без правок). Скрипты — `docs/audit-4/probes/agent_d/` (t1–t12; запуск с python окружения тестов и
`PYTHONDONTWRITEBYTECODE=1`). Строки voice-in — по рабочему дереву на момент аудита; core/core.py — по `3c7b365`.

Audit of voice-in, Speaker, headset and services: 21 findings (worst first) and a list of behaviours that could not be tested.

## Findings

**1. HIGH — A GPU/CUDA failure or hang (the sleep/resume incident) never heals itself.**
- **Where:** `voice_in.py:193-199` (model init), `:600-601` (`to_thread` while holding `ear.lock`), `:626-628`, `:808/811/824`; `services/ksenia-voice-in.service:5-8`; `run_voice_in.sh`.
- **Inference raises (bad CUDA context after resume):**
  - Every /listen returns 500, and the core says "слух не отвечает. Проверь сервис" every time.
  - The live stream dies (see #5).
  - The process stays alive, so `Restart=on-failure` never fires.
- **Inference hangs:** the `to_thread` call never returns, so `ear.lock` (or `ear.streaming`) is held forever.
  - From then on every /listen gets 409, and the core says "Я ещё дослушиваю прошлую фразу" forever.
  - /stream answers "busy". The port is still up, so nothing restarts the service.
- **Init hangs:** with `Type=simple` and no watchdog, the process lives with no port and is never restarted. SIGTERM is not handled while the main thread is inside CUDA C code.
- **Init raises:** systemd crash-loops it every 10 s with `asr_device: cuda`. There is no automatic CPU fallback, although GigaAM on CPU takes about 0.1 s.
- **voice-out has the same gap:** a hung s2 server just makes Speaker wait (#12).
- **Confirmed:** the stuck-lock mechanism (t8b, same path as #4); aiohttp 3.14 does not cancel a handler when the client disconnects. Init paths: reading only.
- **Fix:**
  - Wrap each inference in `asyncio.wait_for(..., 10)`. On timeout or a CUDA error, log and `os._exit(1)`.
  - In `Ear`, try the CUDA providers, and on an exception load with CPU only.
  - In `run_voice_in.sh`, run `timeout 10 nvidia-smi -i 1` (CUDA_VISIBLE_DEVICES=1 → GPU1, the 2080 Ti) and fall back to CPU if it fails.
  - Use `Type=notify` + `WatchdogSec`, or a loop-heartbeat thread that exits the process.

**2. HIGH — When a voiceprint exists, a short "да" from any voice still confirms a risky action.**
- **Where:** `voiceprint.py:43-44` returns `None` for audio under 0.6 s, so `check()` (`:59-60`) gives `{"owner": None, "enrolled": True}` with no `confirm_ok`. In core.py:1060-1061, `guest = owner is False` is False and `weak_voice = enrolled and owner and ...` is None, so the "да" goes to `_resolve_confirmation` and the action runs.
- **Live mode:**
  - A crisp 0.2 s "да" gives a 0.58 s utterance, which is not checked at all.
  - A 0.3–0.4 s "да" gives 0.68–0.78 s of mostly pre-roll, so the check is unreliable.
- **Confirmed** (t7, t7b): `vp.check(0.55 s) -> {'owner': None, 'enrolled': True}`, `guest=False weak_voice=None`.
- **Fix:** in core, require `confirm_ok is True` whenever `enrolled` (treat None as weak). Optionally compute the embedding on the utterance plus its context.

**3. HIGH if guest/owner_only mode is used (MEDIUM otherwise) — In step mode the voiceprint mostly hears silence.**
- **Where:** `record_utterance` returns every frame since parec started (`voice_in.py:377`): the beep tail plus up to 12 s of silence or JBL zeros before he speaks. `voiceprint.py:45-47` then takes the *middle* 8 s.
- **Confirmed** (t3): a 7 s pause then 3 s of speech gives a window over 1.10–9.10 s. It holds only 2.10 of the 3 s of speech and is 74% silence.
- **Consequence:** the embedding is dominated by silence, so Alexander's score likely drops and he gets `owner False`. Then actions are refused as for a guest, or in owner_only mode he is ignored. Enrollment centroids get the same contamination. Score effect not measured without the model.
- **Fix:** trim the pcm to start 0.3 s before `t_speech`. In `embed()`, keep only voiced, non-zero frames before cropping.

**4. MEDIUM-HIGH — The beep `pacat` is awaited with no timeout, so hearing can lock up forever.**
- **Where:** `voice_in.py:293-300` (step mode) and `:739-746` (live).
- **Scenario:** the headset sink whose transport never starts — exactly the 2026-10-09 failure `headset.py` was written for. `p.wait()` never returns. The handler keeps `ear.lock` (or `ear.streaming=True`) after the core gives up, so every later /listen gets 409 and /stream answers "busy" until a manual restart.
- **Confirmed** (t8b, with a fake pacat that never exits): `1st /listen: client gave up` → `2nd /listen -> 409 {"error": "busy"} | ear.lock held: True`.
- **Fix:** `await asyncio.wait_for(p.wait(), 2)`, kill on timeout, and trigger `headset.check_and_recover`.

**5. MEDIUM — Any exception inside the live loop ends live mode in silence.**
- **Where:** `voice_in.py:808, 811, 824, 831` — `transcribe`, `turn_check`, `transcribe_sure`, `await spk` (vp.check). Only `ConnectionResetError` and `CancelledError` are caught (`:834`).
- **What happens:** the core receives a bare CLOSE with no error event. core.py:2222-2224 logs "слух закрыл поток" and returns without saying anything. Alexander hears nothing; live mode is simply off.
- **Confirmed** (t4, transcribe raising a CUDA error): the client got `ready, speech_start, CLOSE`. parec was killed and `streaming` reset correctly.
- **Fix:** catch `Exception` per event and send `{"type":"error","reason":"asr_failed"}`; treat a failed vp.check as `owner=None`. In core, speak a notice when the stream closes.

**6. MEDIUM — In live mode an utterance never ends while any steady sound continues.**
- **Where:** `LiveSegmenter.push` (`voice_in.py:426-439`) checks `max_s` only on silent frames.
- **Why:** JBL's digital zeros drive the noise estimate to 0. The end-of-speech threshold is then `max(0, 0.008*0.7) = 0.0056` (−45 dBFS), and the estimate is frozen while he speaks. A TV, kettle, vacuum or another person starting after he speaks keeps the utterance open with no limit.
- **Confirmed** (t2): 1 s of speech then 300 s of background at 0.02 RMS gives events `{'start', 'partial'×19, 'long'}` and no `end`. 301 s (9.6 MB) was buffered.
- **What he hears:** no answer. About 60 s later the core's `live_idle_s` closes live mode silently.
- **Note:** the noise estimate drifting *up* is not a risk — it only rises on frames below 3× the estimate, cannot overshoot the quiet background, and decays with τ ≈ 0.4 s.
- **Fix:** enforce `max_s` on every frame (and lower it to about 30–45 s in live mode). Re-estimate the floor during speech, for example from the 10th percentile of the last 2 s.

**7. MEDIUM — Step-mode `max_s` (120 s in config.json) is longer than the core's /listen timeout (90 s, core.py:1865).**
- **Scenario:** a long dictation, or background noise that keeps the utterance open.
- **What he hears:** at 90 s the core says "Я тебя не слышу: слух не отвечает. Проверь сервис" and ends the conversation. voice-in then sees the client gone and discards the recording.
- **Confirmed** (t3): speech plus background gives `audio_s: 120.02`.
- **Fix:** set the core timeout to at least `start_timeout_s + max_s + 20` (about 160 s), or lower `max_s`.

**8. MEDIUM — The "hanging word" check runs before the question/exclamation/short-answer check.**
- **Where:** `turn_policy` `voice_in.py:170-175` and step mode `:367-370`. `HANGING_WORDS` (`:136-141`) contains что, как, где, это, так, ты, тебе, ещё, давай.
- **Effect:** "Что?", "Как?", "Что это?", "А ты?", "Почему так?" and "Давай." / "Да, давай!" (as an answer to Ksenia's question) all wait 3 s. That contradicts the docstring ("вопрос … — конец сразу").
- **Confirmed** (t1, t2, t3c):
  - Live mode: "Что это?" and "Давай." ended 2.98 s after he stopped, versus 0.38 s for "Который час?".
  - Step mode: "Что?" ended after 3.00 s, versus 0.80 s.
- **Fix:** evaluate `strong` first (ends with ?/! and not with …/,, or `asked` + `SHORT_ANSWER`). Make `hanging()` return False for text ending in ?/!.

**9. MEDIUM — After "стоп" he hears 0.5 s more of full-volume speech, then a hard cut; the soft fade is never heard.**
- **Where:** core.py:642-658 (`_play_loop`), 719-739 (`_fade_out`), 741-763 (`cancel`).
- **Why:** `_play_loop` keeps writing as long as `drain()` allows, so up to about 1.5 s of audio sits in the asyncio buffer and the 64 KiB pipe. The fade tail is appended *behind* that backlog. `cancel` waits 0.5 s for pacat and then kills it.
- **Confirmed** (t5, with a real-time fake pacat): `cancel() took 0.50s; audio played after cancel: 0.50s (full-level 0.50s, fade blocks 0)`. The old behaviour was an immediate kill.
- **Two latent bugs in the queued-audio fade path** (confirmed in t6; they become audible once the backlog is fixed):
  - Gain is applied twice: at night the level steps from 7500 to 5625.
  - After an odd-length HTTP chunk at gain 1.0, the bytes are decoded misaligned: the "fade" becomes a 120 ms noise burst at RMS 18836 against a 10000 signal, peaking at 32767.
- **Fix:**
  - Bound the write-ahead to about 150 ms: pace writes by the `_play_end` clock, or `set_write_buffer_limits(high=8192)` plus `F_SETPIPE_SZ`.
  - Build the fade from un-gained, byte-aligned data (track the total bytes written).

**10. MEDIUM — The headset can stay in HFP (phone-call quality) permanently.**
- **Where:** `voice_in.py:576-579, 588`.
- **Scenario:** voice-in is killed mid-/listen (SIGTERM → `os._exit`, a restart, the Nexus Conflicts drop-in), or `bt_node`/`find_source` raise `TimeoutExpired` between `set_profile(HFP)` and the `try`. The card stays in `headset-head-unit`.
  - The next /listen sees `prof == hfp_profile`, so `restore` stays None and A2DP is never restored.
  - Ksenia's voice and music stay mono 16 kHz.
  - `headset.status` even reports that profile as "music" (`headset.py:179`).
- **Confirmed** (t11): three /listen calls made no `set-card-profile` call; the profile stayed `headset-head-unit`.
- **Fix:** if the profile is HFP at the start of /listen or at startup, restore the best `a2dp-*` profile. Move `set_profile(HFP)` inside the `try`.

**11. MEDIUM — A TTS response arriving after "стоп" starts a new player and plays a blip.**
- **Where:** core.py:583. `speak()` calls `_ensure_player()` after the response arrives without checking `cancelled`. The first chunk then goes through `_fade_out` into the fresh pacat.
- **What he hears:** about 90 ms of the next phrase, fading out, after "стоп". This hits `say_notice`/backchannels (nothing cancels them) and the 0.5 s window inside `cancel()`.
- **Confirmed** (t10): `players spawned = 2 | bytes written to the post-cancel player = 8192`.
- **Fix:** `if self.cancelled: return` right after the response arrives, and make `_ensure_player` refuse when cancelled.

**12. MEDIUM — When voice-out fails, a blind user gets silence with no explanation.**
- **Where:** core.py:575-582.
- **Behaviour:**
  - A 500 skips the phrase silently.
  - A hung voice-out costs up to `ClientTimeout(total=120)` *per phrase*, i.e. minutes of silence per reply. In step mode the mic is closed, so only the buttons can stop it.
  - With voice-out down, every notice (`say_notice`) is silent too. `ksenia-announce` falls back to `notify-send`, which he cannot see.
- **Confirmed:** reading only.
- **Fix:** `ClientTimeout(connect=2, sock_read=8)`; stop trying after the first failure in a turn; play a pre-rendered "голос не работает" WAV through pacat.

**13. MEDIUM — Enrollment and owner_only mode can lock Alexander out.**
- **Where:** `voicectl.py:73-75`, core.py:1902-1904 / 1918 / 2182, `voiceprint.py:64-83`.
- **Problems:**
  - Enrollment `start` clears neither the old centroid nor voice-in's `pending`, so stale vectors from an aborted enrollment get averaged in.
  - Only phrases with `owner is not False` are added. If the current print rejects him (the reason to re-enroll, e.g. a print made over HFP now heard over LE Audio), enrollment never progresses.
  - `add_last` uses `self.last`, which is whatever was checked most recently — /transcribe or a live partial included.
  - `save()` does not reject phrases that disagree (low self-similarity).
  - In owner_only mode a false reject is skipped *before* `is_stop`, so even "стоп" or switching mode by voice is ignored.
- **Confirmed:** reading only.
- **Fix:** `start` should clear `pending` and disable owner gating; drop outliers below about 0.5 and refuse to save below about 0.6 self-similarity; give owner_only an escape (fixed phrases, or fall back to guest mode after N rejects).

**14. LOW-MEDIUM — Headset recovery can run for many minutes and repeats with no backoff.**
- **Where:** `headset.py:128-149, 198-212, 234-250`.
- **Problem:** `_wait_connected` loops a fixed number of times regardless of elapsed time. If bluetoothd does not come back after the restart, each `bluetoothctl` call waits its full 20 s timeout. Meanwhile `self.lock` is held, so `set_mode` blocks past the core's 90 s timeout.
- **Repeats:** `watch()` re-triggers the full reconnect plus `systemctl restart bluetooth` every minute with no backoff, and a failed recovery is not announced.
- **Confirmed** by simulation (t9): `restart_bluetooth -> False, virtual time spent: 10.8 min`.
- **Fix:** a deadline-based loop, 5 s bluetoothctl timeouts, backoff after a failure, and a one-time spoken failure message.

**15. LOW — `handle_status` (`voice_in.py:849-852`) runs pactl synchronously on the event loop.**
Up to 3 × 5 s blocks the loop, then `TimeoutExpired` gives a 500. The core's `live_possible` (3 s timeout) then silently chooses step mode. Reading only. Fix: `to_thread` plus try/except.

**16. LOW — /stream's "busy" check (`:709`) comes before up to 10 s of awaits; `ear.streaming=True` is only set at `:735`.**
Two concurrent /stream calls can both start. The first one's `finally` then resets `streaming=False` while the second is still running, so a /listen can run in parallel. Reading only. Fix: claim `streaming` immediately after the check.

**17. LOW — `find_mac` (`headset.py:34-45, 168-171`) caches the first paired device advertising "Audio Sink" forever.**
`headset_mac` is not set in config. If another paired speaker or headset is listed first, recovery, status and mode switching act on the wrong device. Reading only.

**18. LOW — Logs and recordings can grow without bound.**
- voice-in.log, voice-out.log and core.log are appended by systemd with no rotation, and voice-out runs with `S2_CODEC_PROF=1`.
- `Speaker.recorded` (core.py:455) keeps the whole reply in RAM, and `save_recording` (624-635) writes it synchronously on the event loop: a 20-minute reading is about 106 MB, and the 30-file cap allows about 3 GB.
- `Mood._save` (`:503-509`) rewrites the baseline file non-atomically on every utterance.
- `logs/listens` is capped at 30 files (OK).
- Fix: logrotate with copytruncate, cap `recorded`, write in a thread, atomic writes.

**19. LOW — `_apply_gain` (core.py:717) wraps around instead of clipping when gain > 1.**
`set_volume` has no upper clamp. Confirmed (t12): gain 1.5 on −30000 gives +20536. Fix: `np.clip` before `astype`.

**20. LOW — `turn_replay.py` never finds the true end in live-mode recordings.**
Live mode saves each utterance with the tail already trimmed to 200 ms (`voice_in.py:820-822`), so the replay never reaches the 800 ms check. The tool also mutates the global CONFIG. Confirmed (t12): `replay ends found: []`. Fix: append 3 s of zeros before replaying.

**21. LOW — Fire-and-forget tasks without kept references, and an unreaped subprocess.**
`voice_in.py:799` and `headset.py:245` create tasks without keeping a reference — the same issue just fixed in core (3c7b365). `headset.run()` kills on timeout without `await p.wait()` (`:28-30`).

## Checked and OK
- Live partial transcriptions are limited to one at a time.
- The live `reader()` task handles ping/pong.
- parec is killed in every `finally`.
- Debug WAVs are capped at 30.
- Voiceprint save is atomic (tmp + replace, though without fsync).
- SIGTERM → `os._exit` is fine under systemd (`KillMode=control-group` kills parec/journalctl).
- The `progress()` playhead runs about 0.2–0.3 s ahead of what he hears (Bluetooth latency); rewinding to the sentence start covers that.

## Not tested (no hardware or models)
- **Voiceprint scores:** silence-heavy step-mode embeddings, HFP-enrolled print versus LE Audio speech, a guest's "да" scores.
- **GigaAM on long single-pass audio** (more than about 25–30 s): onnx-asr recommends VAD for long audio, yet `max_s` is 120 and live checks transcribe the whole buffer.
- **Echo of Ksenia's voice in the JBL mic** compared with the 0.0056/0.008 thresholds (if it leaks, utterances stay open while she talks).
- **Whether pacat actually hangs** on a sink with a failed transport (finding #4 depends on it).
- **Locale of pactl/bluetoothctl output:** the parsers expect English "Name:/Profiles:/Active Profile:"; a Russian locale would silently disable live mode.
- **Dual-bearer headset:** if the card lists `bap-duplex` while in A2DP, /listen switches music mode to `bap-duplex` and never back (`:570-574`).
- **Live mode has no `ignore_start_s`,** so the beep tail may set the initial noise estimate.
- **onnxruntime on a CUDA execution-provider failure:** whether it raises or silently falls back to CPU.
- **voice-out log volume** with `S2_CODEC_PROF=1`.
- **Core live mode idles out silently** after 60 s, while the tablet keeps pushing audio.
