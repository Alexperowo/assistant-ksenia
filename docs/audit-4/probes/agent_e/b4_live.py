import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b1_browser as B
from aiohttp import web
import gateway, tempfile
from playwright.async_api import async_playwright
INSTR = """(() => { window.__said = []; window.__vib = [];
  const sp = speechSynthesis.speak.bind(speechSynthesis); speechSynthesis.speak = (u) => { window.__said.push(u.text); };
  navigator.vibrate = (p) => { window.__vib.push(JSON.stringify(p)); return true; };
  window.__ann = []; for (const id of ['announce','announce-now']) new MutationObserver(() => { const t = document.getElementById(id).textContent; if (t) window.__ann.push(id + ': ' + t); }).observe(document.getElementById(id), {childList: true, characterData: true, subtree: true}); })()"""
class LiveCore(B.Core):
    def __init__(self):
        super().__init__(); self.pushed = 0
    async def push(self, req):
        ws = web.WebSocketResponse(); await ws.prepare(req)
        async for m in ws: self.pushed += 1
        return ws
    def app(self):
        a = web.Application()
        a.add_routes([web.get("/client", self.client), web.get("/push", self.push), web.get("/control/state", self.state),
                      web.route("*", "/{t:.*}", self.any)])
        return a
async def snap(page, tag):
    print(tag, "| live aria-pressed:", await page.get_attribute("#live", "aria-pressed"), "| label:", await page.text_content("#live-label"),
          "| state:", await page.evaluate("KS.state"), "| TTS:", await page.evaluate("window.__said"), "| announced:", await page.evaluate("window.__ann"),
          "| vibrate:", await page.evaluate("window.__vib"))
async def main():
    core = LiveCore()
    cr = web.AppRunner(core.app()); await cr.setup(); cport = B.free_port(); await web.TCPSite(cr, "127.0.0.1", cport).start()
    d = tempfile.mkdtemp(dir=B.HERE); open(os.path.join(d, "pin"), "w").write("482913")
    lport = B.free_port()
    cfg = {"data_dir": d, "core_url": f"http://127.0.0.1:{cport}", "voice_in_url": f"http://127.0.0.1:{cport}", "local_port": lport}
    gw = gateway.Gateway(cfg)
    gr = web.AppRunner(gw.app()); await gr.setup(); await web.TCPSite(gr, "127.0.0.1", lport).start()
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=B.CHROME, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(permissions=["microphone"]); page = await ctx.new_page()
        await page.goto(f"http://127.0.0.1:{lport}/")
        await asyncio.sleep(1.0)
        await page.evaluate(INSTR)
        await page.click("#live")
        await asyncio.sleep(1.5)
        await snap(page, "LIVE ON  ")
        print("   frames pushed to voice-in so far:", core.pushed)
        # A) core ends the live conversation on its own (60 s idle / «пока» / Да / Стоп): gateway & tablet are not told
        await core.ws.send_json({"type": "state", "state": "idle"})
        await asyncio.sleep(1.0)
        n = core.pushed; await asyncio.sleep(1.0)
        await snap(page, "CORE ENDED LIVE (idle)")
        print("   frames still streamed in 1 s:", core.pushed - n)
        # B) gateway restarts / Wi-Fi blip: all sockets drop for ~4 s
        await gr.cleanup()
        await asyncio.sleep(4.0)
        await snap(page, "GATEWAY DOWN 4s")
        gw2 = gateway.Gateway(cfg); gr2 = web.AppRunner(gw2.app()); await gr2.setup(); await web.TCPSite(gr2, "127.0.0.1", lport).start()
        await asyncio.sleep(6.0)
        await snap(page, "GATEWAY BACK")
        await br.close()
    os._exit(0)
if __name__ == "__main__":
    asyncio.run(main())
