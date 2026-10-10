# Отчёт помощника C: system / settings / headphones / selfcheck / net_guard / web / browser_core / unblock / запуск музыки / daily

Как прислан (без правок). Скрипты — `docs/audit-4/probes/agent_c/` (`e_*.py`). Номера строк core.py — по `3c7b365`.

## Audit of system/settings/headphones/selfcheck/net_guard/web/browser_core/unblock/music launch/daily

The three worst problems:
- Confirmed actions are cut off at 60 s, so long installs and Wi-Fi rollbacks break.
- `apt-get` can remove the audio, network, GPU or desktop stack.
- The payment ban is not enforced.

All of these were confirmed by running code. The existing tests in scope pass (60 passed).

### HIGH

**1. Confirmed actions are hard-capped at 60 s, which kills long work and the Wi-Fi rollback.** `core/core.py:1002` (`_resolve_confirmation`: `wait_for(item["run"](), timeout=60)`).
- The command timeouts are much longer: install 900, remove 600, update_system 3600, clean 600 (`system.py:58-65`). `_wifi_connect` can run about 120–200 s (`settings.py:142-153`, `_online` 133-139 = 10×(check ≤10 s + 3 s)).
- When the cap cancels a command, `_exec` and `settings._run` do not kill the child process. They only kill on their own inner `TimeoutError` (`system.py:98-102`, `settings.py:43-47`).
- **Install / update scenario.** The user says "обнови систему" then "да". At 60 s Ksenia says "НЕ удалось: сбой: TimeoutError()", but apt-get keeps running in the background.
  - A retry then hits the dpkg lock.
  - If the user powers the computer off, dpkg is interrupted halfway.
  - **CONFIRMED** (scaled cap of 1 s, "apt-get" replaced by `sleep 4`): core message `сбой: TimeoutError()`, and the child was still running afterwards: `True`.
- **Wi-Fi scenario.** The new network associates, but each connectivity check takes about 6 s (no route or DNS).
  - The rollback `nmcli con up id HomeNet` would start at about t=98 s. The cap cancels the task at 60 s, so it never runs.
  - The computer stays on a dead network, and Wi-Fi is its only connection.
  - **CONFIRMED** with a virtual-clock simulation (`e_wifi.py`, scenario D). Scenario A (captive portal): the rollback starts at 58 s, right at the edge.
- **Fix:**
  - Run confirmed long actions as a background task: say "начала, скажу когда закончу", then report the result with `say_notice`.
  - In `_exec` and `_run`, add `finally: if p.returncode is None: p.kill()`.
  - In `_wifi_connect`, put a total deadline of about 20 s on `_online` and do the rollback in a `finally` / `asyncio.shield`.

**2. `apt-get` install/remove can take out audio, network, GPU or desktop.** `system.py:19` (`PKG_RE`), `27-28`, `58-61`, `74-75`, `178`.
- **Trailing hyphen.** `PKG_RE` accepts a trailing `-`, and `apt-get install pkg-` means *remove* pkg. So `install "pipewire-"` (or `"network-manager-"`, `"sddm-"`) gets asked as "Установить «pipewire-»?" — TTS probably won't voice the hyphen — and after "да" it removes the package.
- **PROTECTED only blocks exact names.**
  - `"nvidia-driver"` and `"linux-image-generic"` never match the real package names (`nvidia-driver-580…`).
  - Packages that silence or cut off Ksenia are unprotected: `pulseaudio-utils` (pacat/pactl), `wireplumber`, `libspa-0.2-bluetooth`, `wpasupplicant`, `plasma-workspace`.
- **`-y` lets apt remove packages without asking.**
  - Cascades: `remove libpipewire-0.3-0t64` would remove its dependants.
  - Conflicts (by apt semantics, not run here): `install pulseaudio` would remove `pipewire-pulse`.
  - `clean` runs `autoremove --purge -y` without simulating it first.
- **CONFIRMED:**
  - All of these names pass the checks and produce `apt-get -y -q install pipewire-`.
  - `apt-get -s install curl-` → "REMOVED: curl".
  - `apt-get -s --no-remove install curl-` → "E: … remove is disabled".
  - `apt-get -s remove libcurl4t64` → 20 packages, including LibreOffice.
- **Fix:**
  - Reject a trailing `-` (and a trailing `+` for remove).
  - Add `--no-remove` to install.
  - For remove and clean, run `apt-get -s` first. Refuse if any protected package (matched by prefix) or more than about 3 packages would go, and speak the list in the question.

**3. The payment ban is not enforced.** `web.py:23-25`, `165-168`.
- The hard block needs *both* a finance word in the URL *and* `оплат|pay|списать|перевест` in the button text. Otherwise the click only needs "да", or nothing at all.
- **CONFIRMED** (`e_pay.py`):

| Page | Button | What happens |
|---|---|---|
| `market.yandex.ru/my/orders` | «Оплатить заказ» | clicked after "да" |
| `ozon.ru/product/…` | «Купить в 1 клик» | clicked after "да" |
| `shop.example/checkout` | «Продолжить» | clicked immediately, no question |
| `vk.com/app123` | «Подарить подарок за 3 голоса» | clicked immediately (VK votes are real money) |

- A prompt-injected page can also get `web_type` (no confirmation without Enter) plus a «Далее» click, sending data out with no question asked.
- **Fix:**
  - Hard-refuse, with no confirmation, when the text or label matches buy/pay/order/subscribe/votes/gift/price words (`оплат|купить|заказ|оформ|подписк|голос|подар|donat|pay|buy|order|checkout|subscribe|purchase|₽|руб`).
  - On finance-like URLs (also `order|zakaz|purchase|vkpay`), refuse every click and every `web_type`.

### MEDIUM

**4. net_guard's plain-HTTP path checks only the first request on a connection.** `net_guard.py:100-110`, `125`; the comment at `:108` claims one request per connection.
- After forwarding the first request, the proxy pipes all further client bytes to the *same* upstream.
- **CONFIRMED** with raw sockets (`e_keepalive.py`). Requests for `http://victim.example/account` (with `Cookie: session=SECRET`) and `http://127.0.0.2:8080/admin` both reached the first, attacker-controlled server.
- This does not reach the LAN. It does leak non-Secure cookies, and the attacker can answer as any `http://` origin. Chromium's actual connection reuse is untested (no browser available).
- **Fix:** forward only the first request's body, and inject `Proxy-Connection: close` / `Connection: close` into the response head.

**5. A failed system change reaches the model as "НЕ удалось: None".**
- `_run_change` returns only `output_tail` / `exit_code`, with no `"error"` key (`system.py:167`). Core formats `res.get('error')` (`core.py:1011`).
- So the dpkg lock, "sudo: a password is required" and "Unable to locate package" are never explained to the user.
- **CONFIRMED.** **Fix:** add `"error"`, mapping the common apt messages to Russian.

**6. The `sudo -n env DEBIAN_FRONTEND=… apt-get` call** (`system.py:27-28, 154, 160-162`).
- sudo checks the command `env`, not `apt-get`. So it works only with `NOPASSWD: ALL` or `NOPASSWD: /usr/bin/env`, which is effectively passwordless root for every process of that user.
- With a sudoers rule for apt-get only, every change fails.
- The repo has no sudoers instructions. **Reading only.**
- **Fix:** a root-owned wrapper `/usr/local/sbin/ksenia-pkg {install|remove|upgrade} NAME` with fixed options, and sudoers for that wrapper only.

**7. dpkg prompts and removals during upgrades** (`system.py:154-158`).
- There is no `-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold`, and stdin is `/dev/null` under systemd. A modified conffile gives "end of file on stdin at conffile prompt", leaving packages half-configured.
- `full-upgrade -y` may also remove packages.
- **Reading only.** **Fix:** add those options; use `upgrade --with-new-pkgs`, or simulate first.

**8. A confirmed web click is matched again by text, not by the element that was confirmed** (`web.py:256-260`, `159-170`).
- **CONFIRMED:** the user was asked «Удалить фото». The page re-rendered at the same URL, and after "да" the click went to «Удалить страницу навсегда».
- **Fix:** pass the confirmed label and refuse if the re-found element's label differs.

**9. A pending confirmation outlives internal turns** (`core.py:1068-1077`, TTL 180 s at `confirm.py:32`).
- The user stays silent after "Переключить Wi-Fi?" or "Установить X?".
- A reminder or watch turn then asks something like "Повторить через 10 минут?". The user's "да" executes the pending install or Wi-Fi switch instead.
- **Reading only.** **Fix:** cancel or mark stale any pending item once Ksenia says anything else after the question; cut the TTL to about 60 s.

**10. `setting mute` and `volume_set 0` silence Ksenia herself** (`settings.py:167-175`).
- They act on `@DEFAULT_AUDIO_SINK@`, which is normally the bluez headphones that core's `pacat` speaks into. A blind user then gets no feedback at all.
- **CONFIRMED** (argv `wpctl set-mute @DEFAULT_AUDIO_SINK@ 1`; `0.00` accepted).
- **Fix:** keep volume at least about 10%, and send "mute" to mpv (the music) rather than the sink.

**11. YouTube `video=true` never shows video** (`music.py:279`, `301-322`).
- `if video and not CONFIG_VIDEO["on"]` is inverted, so the full-screen branch only runs when video is switched *off*.
- **CONFIRMED:** with on=True, `video=True` plays audio only.
- **Fix:** split it into `if video and not on: video = False` followed by a separate `if video:` block.

**12. Wi-Fi decision and rollback are fragile** (`settings.py:124-153`).
- `_online` requires the word `full`. If this Kubuntu has no NetworkManager connectivity URI, the check prints `unknown`, and every *working* switch is rolled back (simulated, scenario C).
- The rollback uses the SSID as the connection id (`:150`), so it fails when the profile name differs.
- An SSID containing `:` makes `_wifi_status` raise (`:127-129`), which aborts both `wifi_status` and `wifi_connect`. **CONFIRMED:** `ValueError('…5G')`.
- The failed new profile is never deleted.
- **Fix:**
  - Store the active connection's UUID and roll back with `con up uuid`.
  - Parse with `-g` / escaping.
  - Delete the new profile on rollback.
  - Verify connectivity some other way when the check says `unknown`.

**13. Up to 60 s of silence after "да".** `_resolve_confirmation` runs before the filler starts (`core.py:1077` vs `1130`). **Fix:** say "Выполняю…" first.

### LOW

14. **Weather geocoder errors look like "city not found"** (`daily.py:145-153`). A 429 or 5xx from the geocoder gives "не нашла город «Москва»", plus a note telling the model to guess another city. **CONFIRMED.** Fix: check `r.status`.
15. **Raw exception text reaches the model and may be read aloud.**
    - headphones when voice-in is down: `ClientConnectorError(ConnectionKey(host='127.0.0.1',…` (**CONFIRMED**).
    - `web_open` goto errors (`net::ERR_TUNNEL_CONNECTION_FAILED`).
    - Bing returning a non-RSS page: `ParseError`.
    - yt-dlp: `TimeoutExpired`.
    - A missing binary in `settings._run` (`:42`, no `FileNotFoundError` handling).
    - `selfcheck` reports `repr(TimeoutError())` as a service state (`selfcheck.py:33`).
16. **`selfcheck._run` does not kill the process on timeout** (`selfcheck.py:30-33`).
17. **WebRTC UDP bypasses net_guard** (`browser_core.py:43-45`). There is no `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`. Untested; no Chromium here.
18. **Some IPv6 forms count as public** (`net_guard.py:27-31`). `64:ff9b::7f00:1` (NAT64), `::7f00:1`, `::ffff:0:7f00:1` and `fec0::1` all pass (**CONFIRMED**). This matters only on NAT64/SIIT networks. Decimal, octal, hex, `127.1`, `::ffff:127.0.0.1` and `nip.io` are all refused (**CONFIRMED**).
19. **The guard allows the house's own public IP** (router WAN, reachable from inside via NAT loopback; rebinding between public addresses is allowed by design). **Reading only.**
20. **Radio stream URLs are not address-checked** (`music.py:178-191`). They are restricted to http(s) only, so mpv will GET LAN or localhost URLs taken from radio-browser. Ksenia's local GET endpoints are read-only.
21. **The unblock bridge tunnels anywhere** (`unblock.py:57-72`). It accepts CONNECT to `127.0.0.1:18100`, `192.168.1.1:80`, `[::1]:18130` and `169.254.169.254` (**CONFIRMED**). It listens only on 127.0.0.1 (`:84`) and byedpi uses `-i 127.0.0.1` (`:82`), so it is **not** an open LAN proxy. It needs no authentication, so any local program can use it.
    - No system-wide proxy variables are set in the code or the services. YouTube uses per-file `http-proxy` and `--proxy` only (`music.py:287, 315-317, 328`).
    - Fix (defence in depth): allow only port 443 and public targets.
22. **`notify-send` has no `--` before the text** (`daily.py:109-110`). GLib parses options anywhere, so a reminder text starting with `-` breaks the notification.
23. **The Wi-Fi password appears in plain text in three places:** nmcli's argv (`settings.py:145`), `core.log` (the tool-arguments log line, `core.py:1174`) and `history.json`.
24. **`search_package` accepts a leading `-`** (`system.py:72`). `--full` was passed to `apt-cache` (**CONFIRMED**). Only harmless options are possible.

### Checked and fine
- No `shell=True` anywhere in scope.
- yt-dlp gets the query only after the `ytsearch1:` prefix, so it cannot become an option.
- The mpv page and stream URLs come from yt-dlp output, and IPC `loadfile` is JSON.
- `web_open` rejects `file:`, `javascript:`, `data:` and userinfo URLs.
- DNS rebinding through the guard is defeated: it connects to the address it checked.

### Not tested
- **Real Chromium through net_guard:** iframes, websockets, service workers, connection reuse, WebRTC. `test_net_guard_browser.py` is skipped without `KSENIA_TEST_CHROMIUM` and covers only redirect, img and direct access. Playwright's browser download is blocked here.
- **On the live machine:** whether `nmcli networking connectivity check` prints `full`, the sudoers setup, whether `qdbus6` exists, and how long ddcutil takes against the 30 s limit.
- **No tests exist for:** the Wi-Fi rollback; `_run_change`; YouTube video; `unblock.py`; `selfcheck` and `headphones`; how the confirmation TTL interacts with internal turns.
