import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b1_browser as B, b2_browser as B2
from aiohttp import web
import gateway, tempfile
from playwright.async_api import async_playwright
INSTR = """(() => { window.__gum = []; const orig = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (c) => { try { const s = await orig(c); window.__gum.push(s); return s; } catch (e) { window.__gum.push('ERR ' + e.name); throw e; } }; })()"""
LIVE = "window.__gum.map(s => typeof s === 'string' ? s : s.getTracks().map(t => t.readyState).join())"
async def main():
    core = B2.SlowCore()
    cr = web.AppRunner(core.app()); await cr.setup(); cport = B.free_port(); await web.TCPSite(cr, "127.0.0.1", cport).start()
    d = tempfile.mkdtemp(dir=B.HERE); open(os.path.join(d, "pin"), "w").write("482913")
    lport = B.free_port()
    gw = gateway.Gateway({"data_dir": d, "core_url": f"http://127.0.0.1:{cport}", "voice_in_url": f"http://127.0.0.1:{cport}", "local_port": lport})
    gr = web.AppRunner(gw.app()); await gr.setup(); await web.TCPSite(gr, "127.0.0.1", lport).start()
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=B.CHROME, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(permissions=["microphone"]); page = await ctx.new_page()
        await page.goto(f"http://127.0.0.1:{lport}/#status")
        await asyncio.sleep(1)
        await page.evaluate("document.querySelector('input[name=mode][value=hold]').click()")
        await page.evaluate("location.hash = '#talk'"); await asyncio.sleep(0.6)
        await page.evaluate(INSTR)
        box = await page.locator("#talk").bounding_box()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for i in range(4):
            await page.mouse.down(); await asyncio.sleep(0.05); await page.mouse.up()
            await asyncio.sleep(1.8)
            print(f"after quick tap {i+1}: state={await page.evaluate('KS.state')!r} label={await page.text_content('#talk-label')!r} mic streams={await page.evaluate(LIVE)}")
        await br.close()
    os._exit(0)
asyncio.run(main())
