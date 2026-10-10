import asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b1_browser as B
from aiohttp import web
import gateway, tempfile
from playwright.async_api import async_playwright

class SlowCore(B.Core):
    async def any(self, req):
        body = await req.read()
        self.calls.append((req.path, body[:120]))
        if req.path == "/control/act" and b"headset_fix" in body:
            await asyncio.sleep(4)
        if req.path == "/transcribe":
            return web.json_response({"text": ""})
        return web.json_response({"ok": True, "say": "Готово."})

async def main():
    core = SlowCore()
    cr = web.AppRunner(core.app()); await cr.setup(); cport = B.free_port(); await web.TCPSite(cr, "127.0.0.1", cport).start()
    d = tempfile.mkdtemp(dir=B.HERE); open(os.path.join(d, "pin"), "w").write("482913")
    lport = B.free_port()
    gw = gateway.Gateway({"data_dir": d, "core_url": f"http://127.0.0.1:{cport}", "voice_in_url": f"http://127.0.0.1:{cport}", "local_port": lport})
    gr = web.AppRunner(gw.app()); await gr.setup(); await web.TCPSite(gr, "127.0.0.1", lport).start()
    out = {}
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=B.CHROME, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(permissions=["microphone"]); page = await ctx.new_page()
        await page.goto(f"http://127.0.0.1:{lport}/#control")
        await B.poll(page, "[...document.querySelectorAll('#control-body button')].some(b => b.textContent === 'Замолчи')")
        n0 = len(core.calls)
        await page.evaluate("[...document.querySelectorAll('#control-body button')].find(x=>x.textContent==='Проверить и починить звук').click()")
        await asyncio.sleep(0.3)
        await page.evaluate("[...document.querySelectorAll('#control-body button')].find(x=>x.textContent==='Замолчи').click()")
        await asyncio.sleep(0.3)
        out["while_headset_fix_running_stop_click_sent"] = [c[1][:30] for c in core.calls[n0:] if c[0] == "/control/act"]
        out["toast_after_ignored_click"] = await page.evaluate("document.getElementById('toast').hidden ? '(hidden)' : document.getElementById('toast').textContent")
        await asyncio.sleep(4.5)
        # reduced motion
        await page.emulate_media(reduced_motion="reduce")
        await page.evaluate("location.hash = '#talk'"); await asyncio.sleep(0.3)
        await page.evaluate("document.getElementById('orb').style.setProperty('--level','1')")
        await asyncio.sleep(0.4)
        out["reduced_motion_orb_core_transform_at_level1"] = await page.evaluate("getComputedStyle(document.querySelector('#orb .orb-core')).transform")
        # hold mode quick tap
        await page.evaluate("location.hash = '#status'"); await asyncio.sleep(0.3)
        await page.evaluate("document.querySelector('input[name=mode][value=hold]').click()")
        await page.evaluate("location.hash = '#talk'"); await asyncio.sleep(0.5)
        box = await page.locator("#talk").bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        duck0 = sum(1 for c in core.calls if c[0] == "/duck" and b"true" in c[1])
        await page.mouse.move(x, y); await page.mouse.down(); await asyncio.sleep(0.05); await page.mouse.up()   # quick tap
        await asyncio.sleep(1.5)
        out["hold_after_quick_tap_state"] = await page.evaluate("KS.state")
        await page.mouse.down(); await asyncio.sleep(0.05); await page.mouse.up()   # second tap to finish
        await asyncio.sleep(1.5)
        out["hold_after_second_tap_state"] = await page.evaluate("KS.state")
        await page.mouse.down(); await asyncio.sleep(0.05); await page.mouse.up()   # third tap
        await asyncio.sleep(1.5)
        out["hold_after_third_tap_state"] = await page.evaluate("KS.state")
        out["recordings_started(duck on)"] = sum(1 for c in core.calls if c[0] == "/duck" and b"true" in c[1]) - duck0
        out["utterances_posted"] = sum(1 for c in core.calls if c[0] == "/transcribe")
        await br.close()
    for k, v in out.items(): print(f"{k}: {v}")
    os._exit(0)
if __name__ == "__main__":
    asyncio.run(main())
