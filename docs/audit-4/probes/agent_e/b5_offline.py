import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b1_browser as B, b4_live as L
from aiohttp import web
import gateway, tempfile
from playwright.async_api import async_playwright
async def main():
    core = L.LiveCore()
    cr = web.AppRunner(core.app()); await cr.setup(); cport = B.free_port(); await web.TCPSite(cr, "127.0.0.1", cport).start()
    d = tempfile.mkdtemp(dir=B.HERE); open(os.path.join(d, "pin"), "w").write("482913")
    lport = B.free_port()
    gw = gateway.Gateway({"data_dir": d, "core_url": f"http://127.0.0.1:{cport}", "voice_in_url": f"http://127.0.0.1:{cport}", "local_port": lport})
    gr = web.AppRunner(gw.app()); await gr.setup(); await web.TCPSite(gr, "127.0.0.1", lport).start()
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=B.CHROME, args=["--autoplay-policy=no-user-gesture-required"])
        page = await (await br.new_context()).new_page()
        await page.goto(f"http://127.0.0.1:{lport}/"); await asyncio.sleep(1.0)
        await page.evaluate(L.INSTR)
        t0 = time.monotonic()
        await gr.cleanup()   # PC/gateway off (night), tablet app stays open with wake lock
        await asyncio.sleep(45)
        v = await page.evaluate("window.__vib")
        print(f"gateway down {time.monotonic()-t0:.0f}s: offline vibrations={sum(1 for x in v if x=='[300,100,300]')} all={v} TTS={await page.evaluate('window.__said')}")
        await br.close()
    os._exit(0)
if __name__ == "__main__":
    asyncio.run(main())
