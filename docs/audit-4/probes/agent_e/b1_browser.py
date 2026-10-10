"""Real Chromium against the real gateway + web files, fake core/voice-in. CSP NOT bypassed. Scratch only."""
import asyncio, json, os, socket, sys, tempfile, time
sys.path.insert(0, "/home/user/assistant-ksenia/pwa")
from aiohttp import web
import gateway
from playwright.async_api import async_playwright

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
HERE = os.path.dirname(os.path.abspath(__file__))


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


STATE = {
    "ksenia": {"busy": False, "conversation": False, "speaking": False, "live": False},
    "settings": [{"key": "live_mode", "group": "talk", "type": "bool", "label": "Живой разговор", "value": True}],
    "voice_mode": "guest", "voice_enrolled": False, "voice_enrolling": 0,
    "headset": {"connected": True, "mode": "talk"}, "volume": 40, "muted": False,
    "music": {"playing": True, "station": "Джаз", "paused": False},
    "services": [{"unit": "ksenia-core", "label": "Ядро", "ok": True, "word": "работает"}],
    "memory": ["люблю чай", "кошка Муся"], "rules": [], "reminders": [], "diary": [], "notes": [],
    "report": {"file": "week-1.md", "text": "Сводка: всё хорошо.\n" * 30},
}


class Core:
    def __init__(self):
        self.ws, self.calls, self.down = None, [], False

    async def client(self, req):
        if self.down:
            return web.Response(status=503)
        ws = web.WebSocketResponse(); await ws.prepare(req); self.ws = ws
        await ws.send_json({"type": "hello", "busy": False, "confirm": None})
        async for _ in ws:
            pass
        return ws

    async def state(self, req):
        return web.json_response(STATE)

    async def any(self, req):
        self.calls.append((req.path, (await req.read())[:120]))
        if req.path == "/control/act":
            d = json.loads(await req.read())
            if d.get("action") == "music":
                STATE["music"]["paused"] = d.get("do") == "pause"
            await asyncio.sleep(float(d.get("_sleep", 0)))
        return web.json_response({"ok": True, "say": "Готово."})

    def app(self):
        a = web.Application()
        a.add_routes([web.get("/client", self.client), web.get("/control/state", self.state),
                      web.route("*", "/{t:.*}", self.any)])
        return a


async def poll(page, js, timeout=10):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if await page.evaluate(js):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.1)
    return False


async def main():
    core = Core()
    cr = web.AppRunner(core.app()); await cr.setup()
    cport = free_port(); await web.TCPSite(cr, "127.0.0.1", cport).start()
    d = tempfile.mkdtemp(dir=HERE); open(os.path.join(d, "pin"), "w").write("482913")
    lport = free_port()
    gw = gateway.Gateway({"data_dir": d, "core_url": f"http://127.0.0.1:{cport}", "voice_in_url": f"http://127.0.0.1:{cport}",
                          "local_port": lport})
    gr = web.AppRunner(gw.app()); await gr.setup(); await web.TCPSite(gr, "127.0.0.1", lport).start()
    base = f"http://127.0.0.1:{lport}/"
    out = {}
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=CHROME, args=["--use-fake-ui-for-media-stream",
                                     "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(permissions=["microphone"])
        page = await ctx.new_page()
        logs = []
        page.on("console", lambda m: logs.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
        await page.goto(base)
        ok = await poll(page, "!document.getElementById('app').hidden")
        await asyncio.sleep(0.5)
        out["app_shown"] = ok
        out["ws_clients_at_gateway"] = len(gw.fanout.queues)

        # ---- 1. stale confirm after core restart ----
        await core.ws.send_json({"type": "confirm", "label": "x", "question": "Пишу Диме: привет. Отправить?"})
        await poll(page, "!document.getElementById('confirm').hidden")
        await core.ws.close()  # core restarts; on reconnect sends hello with confirm=None
        await asyncio.sleep(3.5)
        out["confirm_visible_after_core_restart_with_no_pending"] = await page.evaluate("!document.getElementById('confirm').hidden")

        # ---- 2. status view: <details> weekly report collapses on 6 s refresh ----
        await page.evaluate("location.hash = '#status'")
        await poll(page, "!!document.querySelector('#status-body details')")
        await page.evaluate("document.querySelector('#status-body details').open = true")
        await page.evaluate("window.__rep = document.querySelector('#status-body details')")
        await asyncio.sleep(7)
        out["report_details_open_after_7s"] = await page.evaluate("!!(document.querySelector('#status-body details') || {}).open")
        out["report_node_replaced"] = await page.evaluate("window.__rep !== document.querySelector('#status-body details')")

        # ---- 3. memory view: focus on a non-button item lost on refresh; same-node? ----
        await page.evaluate("location.hash = '#memory'")
        await poll(page, "!!document.querySelector('#memory-body li')")
        await page.evaluate("window.__li = document.querySelector('#memory-body li')")
        await asyncio.sleep(7)
        out["memory_li_replaced_after_7s"] = await page.evaluate("window.__li !== document.querySelector('#memory-body li')")

        # ---- 4. control view: focus after toggling music pause (label changes) ----
        await page.evaluate("location.hash = '#control'")
        await poll(page, "[...document.querySelectorAll('#control-body button')].some(b => b.textContent === 'Пауза')")
        btn = page.locator("#control-body button", has_text="Пауза")
        await btn.focus(); await btn.press("Enter")
        await asyncio.sleep(1.5)
        out["focus_after_pause_toggle"] = await page.evaluate("document.activeElement.tagName + ':' + (document.activeElement.textContent||'').slice(0,30)")

        # ---- 5. busy act swallows a second action silently ----
        await page.evaluate("""KS.api('/api/control/act', {action:'noop'})""")
        n0 = len(core.calls)
        await page.evaluate("""(() => { const b=[...document.querySelectorAll('#control-body button')].find(x=>x.textContent==='Проверить и починить звук'); b.click(); })()""")
        await asyncio.sleep(0.1)
        await page.evaluate("""(() => { const b=[...document.querySelectorAll('#control-body button')].find(x=>x.textContent==='Замолчи'); b.click(); })()""")
        await asyncio.sleep(0.5)
        out["acts_sent_for_two_clicks"] = [c[1][:40] for c in core.calls[n0:]]

        # ---- 6. reduced motion: orb still scales with --level ----
        await page.emulate_media(reduced_motion="reduce")
        await page.evaluate("location.hash = '#talk'")
        await asyncio.sleep(0.3)
        out["reduced_motion_orb_core_transform"] = await page.evaluate(
            "document.documentElement.style.setProperty('--level','1'); getComputedStyle(document.querySelector('#orb .orb-core')).transform")
        out["reduced_motion_orb_core_transition"] = await page.evaluate("getComputedStyle(document.querySelector('#orb .orb-core')).transition")

        # ---- 7. SW offline fallback for /control.js ----
        sw_ready = await poll(page, "navigator.serviceWorker && navigator.serviceWorker.controller !== null", 8)
        out["sw_controls_page"] = sw_ready
        await gr.cleanup()   # gateway down (PC rebooting / Wi-Fi lost)
        logs.clear()
        try:
            await page.reload(timeout=8000)
        except Exception as e:
            out["reload_err"] = str(e)[:80]
        await asyncio.sleep(2.5)
        out["offline_reload_nolink_visible"] = await page.evaluate("!document.getElementById('nolink').hidden")
        out["offline_announce_regions"] = await page.evaluate("[document.getElementById('announce-now').textContent, document.getElementById('announce').textContent]")
        out["offline_activeElement"] = await page.evaluate("document.activeElement.tagName")
        out["offline_console"] = [l for l in logs if "control.js" in l or "MIME" in l or "Refused" in l][:3]
        out["csp_or_errors_seen"] = [l for l in logs if "Content Security Policy" in l or "pageerror" in l][:5]
        await br.close()
    await cr.cleanup()
    for k, v in out.items():
        print(f"{k}: {v}")
    os._exit(0)

if __name__ == "__main__":
    asyncio.run(main())
