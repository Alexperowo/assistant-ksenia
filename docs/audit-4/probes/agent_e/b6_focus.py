import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import b1_browser as B
from aiohttp import web
import gateway, tempfile
from playwright.async_api import async_playwright
async def main():
    core = B.Core()
    cr = web.AppRunner(core.app()); await cr.setup(); cport = B.free_port(); await web.TCPSite(cr, "127.0.0.1", cport).start()
    d = tempfile.mkdtemp(dir=B.HERE); open(os.path.join(d, "pin"), "w").write("482913")
    lport = B.free_port()
    gw = gateway.Gateway({"data_dir": d, "core_url": f"http://127.0.0.1:{cport}", "voice_in_url": f"http://127.0.0.1:{cport}", "local_port": lport})
    gr = web.AppRunner(gw.app()); await gr.setup(); await web.TCPSite(gr, "127.0.0.1", lport).start()
    async with async_playwright() as p:
        br = await p.chromium.launch(executable_path=B.CHROME, args=["--autoplay-policy=no-user-gesture-required"])
        page = await (await br.new_context()).new_page()
        await page.goto(f"http://127.0.0.1:{lport}/#control")
        await B.poll(page, "!!document.querySelector('#control-body input[role=switch]')")
        sw = page.locator("#control-body input[role=switch]").first
        await sw.focus(); await page.keyboard.press("Space"); await asyncio.sleep(1.0)
        print("after toggling a setting switch, activeElement:", await page.evaluate("document.activeElement.tagName + (document.activeElement.id ? '#'+document.activeElement.id : '')"))
        r = page.locator("#control-body input[type=radio]").nth(1)
        await r.focus(); await page.keyboard.press("Space"); await asyncio.sleep(1.0)
        print("after picking a radio option, activeElement:", await page.evaluate("document.activeElement.tagName"))
        await page.evaluate("location.hash = '#talk'"); await asyncio.sleep(0.5)
        await core.ws.send_json({"type": "confirm", "label": "x", "question": "Отправить?"})
        await B.poll(page, "!document.getElementById('confirm').hidden")
        await page.focus("#yes"); await page.keyboard.press("Enter"); await asyncio.sleep(0.5)
        print("after pressing «Да», activeElement:", await page.evaluate("document.activeElement.tagName + (document.activeElement.id ? '#'+document.activeElement.id : '')"))
        await br.close()
    os._exit(0)
asyncio.run(main())
