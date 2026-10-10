import sys, asyncio
sys.path.insert(0, "/home/user/assistant-ksenia/core"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
from tools import browser_core, confirm, web
from fake_page import FakePage
holder = {}
async def fake_page(purpose): return holder["page"]
browser_core.page = fake_page
def scenario(url, el, text):
    confirm.cancel()
    holder["page"] = pg = FakePage(url, [el]); web._state["url"] = url
    r = asyncio.run(web.call("web_click", {"text": text}, None))
    after = None
    if r.get("prepared"):
        after = asyncio.run(confirm.take()["run"]())
    print(f"{url:45s} {text!r:22s} -> {({k: r[k] for k in r if k in ('ok','error','speak_verbatim','clicked')})} ; after 'да': {after and after.get('ok')} ; clicks={pg.log}")
scenario("https://market.yandex.ru/my/orders", {"role": "button", "text": "Оплатить заказ"}, "Оплатить заказ")
scenario("https://www.ozon.ru/product/12345", {"role": "button", "text": "Купить в 1 клик"}, "Купить в 1 клик")
scenario("https://shop.example/checkout", {"role": "button", "text": "Продолжить"}, "Продолжить")
scenario("https://www.avito.ru/item/1", {"role": "button", "text": "Оформить и оплатить"}, "Оформить")
scenario("https://vk.com/app123", {"role": "button", "text": "Подарить подарок за 3 голоса"}, "Подарить")
