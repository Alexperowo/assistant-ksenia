import sys, asyncio
sys.path.insert(0, "/home/user/assistant-ksenia/core"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
from tools import browser_core, confirm, web
from fake_page import FakePage
pg = FakePage("https://site.example/profile", [{"role": "button", "text": "Удалить фото"}])
async def fake_page(p): return pg
browser_core.page = fake_page; web._state["url"] = pg.url
r = asyncio.run(web.call("web_click", {"text": "Удалить"}, None))
print("asked:", r["speak_verbatim"])
# same URL, the SPA re-rendered (a dialog opened / list changed) before Alexander said «да»
pg.elements = [{"role": "button", "text": "Удалить страницу навсегда"}, {"role": "button", "text": "Удалить фото"}]
print("after «да»:", asyncio.run(confirm.take()["run"]()), pg.log)
