# Отчёт помощника A1: core/core.py (ход, история мозга, песочница, напоминания), diary.py, speech_norm.py

Как прислан (без правок). Опыты — `docs/audit-4/probes/agent_a1/test_audit_a1.py` (20 тестов, каждый
показывает ошибку; запускались на `git archive d93272b`). Номера строк — по `origin/main` = d93272b.

**Baseline.** Line numbers are from `origin/main` = d93272b, the 2665-line core.py. Commit 4c58513 on the audit branch fixes finding 7 (system prompt / restart recompute).

## Findings, most severe first

**1. HIGH — A "да" meant for a different question runs the pending risky action.** core.py:2223-2229, called right after every turn (1827, 1958), plus `_resolve_confirmation` at 907-931.
- Scenario: Ksenia asks "Отправить Маше…?". `deliver_waiting` then immediately speaks a finding or VK notice that ends with its own question ("Рассказать подробнее?"). Alexander answers "да" to that, and the core sends the VK message (or trashes the file, installs, clicks).
- Internal turns deliberately keep the pending confirmation, and `test_core_internal.py:27` enshrines that.
- **Confirmed (E2):** `sent=['SENT']`, note "Александр подтвердил, ядро ВЫПОЛНИЛО…".
- Fix: don't deliver `waiting` items while `confirm.current()` is pending, or re-speak the question verbatim after the internal turn.

**2. HIGH — `is_affirmative` accepts replies that are not consent.** core.py:1678-1689.
- **Confirmed:** all of these return True: "Давай другой текст", "давай потом", "давай позже", "давай завтра", "отправь потом", "да ладно", "да ну", "ага, щас", "да? а кому?", "Окей, а кто это?", "можно потом?".
- Fix:
  - Reject anything containing "?".
  - Add to NEGATE: потом, позже, завтра, другой, другое, иначе, щас, ладно (when it follows "да"), ну (when it follows "да").
  - For words after the first, allow only a small whitelist (отправляй, конечно, можно…).

**3. HIGH — Confirmed actions are cut at 60 s.** `wait_for(item["run"](), 60)` at core.py:922.
- `system` install and update have their own budgets of 900–3600 s. After 60 s the core tells the model "НЕ удалось: сбой: TimeoutError()" while apt keeps running.
- `_exec` (tools/system.py:98-102) kills the process only on TimeoutError, not on cancel. The orphan dies whenever the transport is garbage-collected, possibly mid-dpkg.
- Those 60 s are silent: the filler task only starts at 1047, after resolution.
- **Confirmed (E13, 60 s scaled to 1 s):** the note says "НЕ удалось" while the process is still alive (pgrep found it).
- Fix: use a per-action timeout, say "Начинаю, это займёт пару минут", run in the background and report through `waiting`, and kill the subprocess on CancelledError too.

**4. HIGH — Reminders are lost when their delivery is cancelled or cut.** core.py:2227 pops the item before it is spoken, and `daily.due()` has already deleted it from disk.
- Live mode makes this likely. The idle tick starts `live_waiting` whenever the turn is not busy (2120-2121), even while Alexander is mid-sentence. His utterance then cancels the turn (`cancel_turn`), and the reminder is never said.
- A button press (`start_talk` → `ks.stop`) cuts it the same way. `remember_interruption` is skipped for internal turns (1117), so "продолжай" can't recover it.
- If he spoke within 8 s of his previous utterance, that old utterance is also re-sent glued to the new one (2079-2081).
- **Confirmed (E5):** `waiting=[]`, spoken=[].
- Fix: peek instead of pop and remove only after it was spoken; don't start delivery after `speech_start` until the utterance arrives.

**5. HIGH — With the brain down or erroring, a reminder's text is never spoken.** The internal turn only says BRAIN_FAIL_PHRASE (1243-1244), and the reminder is already gone from disk. Voice-out failure loses it the same way.
- **Confirmed (E6):** spoken = only "Ой, у меня что-то с головой…".
- Fix: if an internal reminder turn fails, `say_notice(f"Напоминание: {text}")` directly, and keep a retry.

**6. HIGH (needs a live check) — A barge-in makes the history differ from what the brain has cached in slot 0.**
- In `_step` (1185-1188), `if speaker.cancelled: break` runs before the line just read is parsed, and the server has generated further tokens anyway. The stored assistant message is therefore shorter than the cached token stream.
- By the project's own model (hybrid brain, no checkpoints in the PrismML fork, as noted in `warmup()` at 2504-2506), the next turn is not an extension and triggers a full recompute of about 13 s after every interruption. Interruptions are the core of live mode. The same applies to a failed mid-stream step.
- **Confirmed by code (P1):** the server generated at least "Рим основан в 753 году до нашей эры."; the history stores "Рим основан в 753 году".
- Verify with REVIEW.md live check 9 in brain.log.
- Fix: after a cancel, keep draining the stream (audio discarded) and store the full content and tool_calls.

**7. HIGH on main, fixed on the branch — The system prompt is rebuilt after pauses.** core.py:973-976 rebuilds it when diary or memory changed and 5 min have passed. `diary_loop` (2579-2595) writes an entry after every 10-minute pause with 3 or more turns, so the first reply after almost every break recomputes everything. A restart has the same effect (859 rebuilds the prompt; `window_start=0` causes a jump). Commit 4c58513 addresses this. Found by reading.

**8. MEDIUM-HIGH — The history window jumps (full recompute) about every 11 turns.** `_window` at 933-955 with `history_max: 60` in core/config.json.
- **Confirmed (P3, half the turns with a tool):** jumps at turns 20, 31, 42, 53, 64, 75.
- Fix:
  - Raise `history_max` and `history_max_chars` (each slot has about 65k tokens).
  - Jump only when idle unless a hard cap is hit.
  - Pre-fill the new prefix while idle on slot 0 with `/completion n_predict=0` on the rendered history without a generation prompt. Because nothing is generated, the next request is a pure extension, which makes this a safe warm-up (it also answers the "13 s after restart" item in the task).

**9. MEDIUM-HIGH — Sandbox turns leak into the real conversation.** turn 1343-1359 and respond.
- (a) `_resolve_confirmation` runs in the sandbox. It cancels Alexander's real pending confirmation (E4a: pending=None), or executes it if the sandbox text is affirmative (E4b: SENT). *(исправлено в 30a6a72)*
- (b) Tools that call `confirm.ask` are not in SANDBOX_STUB (line 128): files move/trash, screen_click, ui_click/ui_type, web_click/web_type. They register a real pending action and show Да/Нет on the tablet; Alexander's next "да" executes it (E4c: TRASHED).
- (c) The sandbox consumes the real "тебя перебили" note and NEW_TOOLS (1010-1021). E4d: real turn note empty, `noted=True`, `NEW_TOOLS=None`.
- (d) A "стоп" during a sandbox turn sets `ks.interrupted` to sandbox text (1117-1118), and `can_resume("продолжай")` is True. The real conversation would then speak the sandbox reply and write it into history (E4e).
- (e) Real side-effect tools still run: music_play, youtube, app_open, headphones fix/mode, window_action, magnifier.
- Fix: in sandbox mode skip resolution, snapshot and restore `confirm._pending`, `interrupted`, NEW_TOOLS and the tag/offer state, and replace the deny-list with an allow-list of read-only tools.
- Related, low: importing core rewrites `data/tools_hash.json` (43-63), so running pytest on Alexander's PC before restarting the core also eats the "умения обновились" note.

**10. MEDIUM — The `/say` handler with `sandbox=true` still disturbs the room.** core.py:1383-1400.
- It calls `ks.stop()` (1389), cutting Ksenia's real reply mid-word.
- It ducks the music (1393).
- It sets the global `TEST_SINK` (1394). Any Speaker opened while the selftest request waits or runs goes silently into the null sink: say_notice "Записала.", LISTEN_DOWN, "Отдыхаю.", the startup check. *(TEST_SINK исправлен в 30a6a72)*
- Fix: skip stop and duck for the sandbox, and pass the sink per Speaker. Found by reading.

**11. MEDIUM-HIGH — Memory can be written without confirmation.** core.py:986-987 sets `confirm.CONTEXT["affirmative"]` from any "да/давай/ага"; tools/memory.py:74-77 trusts it for the whole turn, as it does any user text containing "запомн".
- Scenario: "Прочитать статью?" → "давай" → the page says "запомни: …" → `memory_remember` stores it with no question. That is a permanent injection into the system prompt.
- **Confirmed (E3):** the fact was saved and no confirmation was pending.
- Fix: count "affirmative" only when it actually resolved a pending "запомнить…" item.

**12. MEDIUM — In live mode, a "да" spoken while the asking turn is still busy is dropped.** core.py:2049-2054; `live_intent.quick` classifies "да" during busy as "continue".
- Busy covers the TTS of the verbatim question and the model's next step. The "да" is lost, Alexander hears nothing, and the confirmation stays armed for a later unrelated "да" (feeds finding 1).
- **Confirmed (E8):** no turn launched, still pending.
- Fix: while a confirmation is pending, route yes/no replies to `turn()`.

**13. MEDIUM — Cancelling during a confirmed action loses the record.** core.py:918-925, with resolution happening before the user message is appended at 1026.
- `take()` already popped the item, and CancelledError bypasses `except Exception`. The "да" turn is never written to history, so it is unknown whether the message went out; the model may re-ask, leading to a double send.
- **Confirmed (E7):** action state `['request sent']`, history grew by 0.
- Fix: `asyncio.shield` the action, and append the user turn and result note even on cancel.

**14. MEDIUM — A weak-voice "да" on an expired confirmation crashes the turn.** core.py:993 calls `confirm.current()` twice; the first call clears the expired item, so the second returns None and `.get` raises AttributeError.
- Classic conversation: "Ой, у меня что-то сломалось" and the conversation ends.
- Live mode: `live_turn` (1952-1960) has no except, so the task dies with total silence.
- Also, the gateway's `hello` message (1625) consumes the expiry, so the model never gets the "устарело" note.
- **Confirmed (E1):** `AttributeError("'NoneType' object has no attribute 'get'")`.
- Fix: read `current()` once, make it side-effect free, and add try/except → `say_notice(CONV_CRASH)` in `live_turn`.

**15. MEDIUM — A down or hung voice-out gives no audible fallback and holds `ks.lock`.** core.py:563-598.
- Each phrase waits up to 120 s (568), plus 15 s of 503 retries (563). `cancel()` does not interrupt a pending POST, because cancellation is only checked between chunks. "стоп" therefore doesn't release the lock, and a 5-phrase answer can hold it for about 10 minutes.
- **Confirmed (E9):** cancel at 0.2 s, `speak()` returned at 3.0 s, when the slow server answered.
- Fix: `sock_connect=2` and `sock_read≈8`, race the POST against a cancel event, skip the remaining phrases after the first failure, and play a pre-rendered "голос не работает" WAV.

**16. MEDIUM — A hung brain means up to 3 minutes of silence.** total=180 at 1180, and cancellation is only seen when a line arrives (1186). After "Хм, секунду", silence lasts up to 180 s, and hush or `/say` can't free the lock during prompt processing. Fix: `sock_read≈30` and a cancel-aware read. Found by reading.

**17. MEDIUM — Live mode stops listening silently when voice-in drops the stream** (crash or restart), at core.py:2126-2128. No phrase is spoken. Fix: say LISTEN_DOWN unless the core closed the stream itself. Found by reading.

**18. MEDIUM — Reminders, findings and VK notices go to the tablet during a headset conversation.** `preferred_output()` (1404-1407) picks the tablet if it was used in the last 10 min, and `deliver_waiting` uses it (2229). Alexander, on the headset, doesn't hear them; findings have no HDMI replay. Fix: when a conversation is active, use its output, and reset `last_client_t` when a headset conversation starts. Found by reading.

**19. MEDIUM — `fix_english_numbers` (core.py:206-219) breaks English titles and sequences. Confirmed:**

| Input | Spoken as |
|---|---|
| «One» группы Metallica | «1» |
| Seven Nation Army | 7 Nation Army |
| Take Five | Take 5 |
| one two three four | 3 7 |
| Five Six Seven Eight | 11 15 |
| eleven two | 13 |

Fix: combine with a units word only after twenty…ninety, and never convert inside «…» or a run of Latin words.

**20. MEDIUM — `feminine()` (229-253) changes nouns and quoted male speech. Confirmed:**

| Input | Spoken as |
|---|---|
| Я сейчас канал переключу | канала |
| Я тут журнал нашла | журнала |
| Я сейчас материал поищу | материала |
| Сергей пишет: «Я приехал, я опоздал» | приехала, опоздала |
| Ты сказал: «я устал» | устала |

Fix: skip text inside quotes, apply the generic -л rule only right after "я" (or use a part-of-speech check such as pymorphy3), and add a stoplist of common nouns ending in -ал/-ел/-ил.

**21. LOW-MEDIUM — Wrong replacements in speech_norm. Confirmed:**

| Rule | Input | Spoken as |
|---|---|---|
| Stress, speech_norm.py:58 | С самого начала / До начала фильма | начала́ (noun genitive is the common case) |
| г., line 84 | 200 г. муки | 200 года муки |
| Degrees, line 86 | Около 1° | Около 1 градус |
| Minus, line 97 | 2 -1 | 2 минус 1 |

**22. LOW-MEDIUM — The first-sentence split cuts abbreviations.** `split_first_sentence` (280-286, used at 1222).
- **Confirmed:** "…давление 745 мм рт. ст., ветер 3 м/с." becomes "…745 миллиметров рт." and then "ст., …"; this is a typical weather answer.
- Fix: don't split after г./рт./ст./н./т./др., or normalize before splitting.

**23. LOW-MEDIUM — The empty-step retry forces a recompute.** core.py:1056-1060 resends the identical prompt after the server already generated reasoning on slot 0; the hybrid brain can't roll that back. The comment claims a cache hit.
- **Confirmed (P2):** same messages, first request `reasoning_effort=low, budget 512`, second `enable_thinking False`.
- Fix: append the generated (reasoning-only) assistant message and continue append-only.

**24. LOW-MEDIUM — The diary loses conversations. Confirmed:**
- (a) A brain error JSON (503 "Loading model") marks the conversation as done with no entry: diary.py:130-137 (E10: upto=10/10, entries=[]).
- (b) Messages appended during the summary request are skipped, because `upto=len(history)` is taken after the await (E11: 10→14).
- (c) `upto` is an index into the in-memory history, but history.json keeps only the last 200 (core.py:874). After a restart, messages are skipped (E12: 30 replies lost).
- (d) "сегодня/вчера" (diary.py:58-61) are frozen into the cached system prompt, so after midnight yesterday's talk is still called "сегодня". This gets worse with 4c58513, which rebuilds the prompt only on window jumps.
- Fix: store a message fingerprint instead of an index, advance only on a valid reply, and use absolute dates.

## Important behaviours with no test coverage
- `_resolve_confirmation`: timeout, cancellation, and actions that run longer than 60 s.
- The weak-voice (`confirm_ok=False`) branch; expired confirmation combined with weak voice.
- `is_affirmative` on postponements and questions.
- A live-mode yes/no while a turn is busy.
- Sandbox interaction with `confirm`, `interrupted`, NEW_TOOLS, side-effect tools, and the `/say` stop/duck/sink handling.
- `deliver_waiting` being cancelled or cut; reminder fallback when the brain or voice is down; `findings_loop`.
- A mid-stream cancel in `_step` (`_stream_cut`) and whether history then equals the generated tokens; the empty-step retry.
- A voice-out hang (no sock_read timeout) and a cancel during the POST; brain stalls with no tokens.
- Live mode: voice-in closing the stream; exceptions inside `live_turn`.
- `preferred_output` during a headset conversation.
- `fix_english_numbers` (no tests at all); `feminine` with nouns or quotes; stress-word homographs; splitting at abbreviations.
- The `_load_history` corruption path; `history_keep` trimming versus the diary's `upto`; diary errors and races.
- `remember_interruption` and `can_resume` are only tested indirectly.
