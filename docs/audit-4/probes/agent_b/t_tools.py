import asyncio, json, sys, time, os, tempfile, re
sys.path.insert(0, "core"); sys.path.insert(0, "tests")
from tools import confirm, memory, daily, desktop, web, browser_core
from fake_page import FakePage
tmp = tempfile.mkdtemp()
memory.FILE = os.path.join(tmp, "mem.json"); daily.FILE = os.path.join(tmp, "rem.json")

# ---- web_click: payment-like buttons on non-finance URLs ----
def click(url, elements, text):
    pg = FakePage(url, elements)
    async def page(_): return pg
    browser_core.page = page
    web._state["url"] = url
    confirm.cancel()
    r = asyncio.run(web.call("web_click", {"text": text}, None))
    return {k: r.get(k) for k in ("ok", "clicked", "speak_verbatim", "error")}, pg.log
cases = [
 ("https://shop.example.ru/order/123", [{"role": "button", "text": "Заплатить 4 990 ₽"}], "Заплатить"),
 ("https://shop.example.ru/order/123", [{"role": "button", "text": "Пополнить кошелёк"}], "Пополнить"),
 ("https://shop.example.ru/item/1", [{"role": "button", "text": "Добавить в корзину"}], "Добавить в корзину"),
 ("https://site.example.ru/post/1", [{"role": "button", "text": "Опубликовать"}], "Опубликовать"),
 ("https://yoomoney.ru/transfer", [{"role": "button", "text": "Перевести"}], "Перевести"),
 ("https://market.example.ru/my/orders/confirmation", [{"role": "button", "text": "Оплатить"}], "Оплатить"),
 ("https://form.example.com/step1", [{"role": "button", "text": "Продолжить"}], "Продолжить"),
]
for url, els, t in cases:
    print("web_click", url, t, "->", click(url, els, t))

# after 'да' on non-finance URL: executes payment click
pg = FakePage("https://market.example.ru/my/orders/confirmation", [{"role": "button", "text": "Оплатить"}])
async def page(_): return pg
browser_core.page = page; web._state["url"] = pg.url; confirm.cancel()
asyncio.run(web.call("web_click", {"text": "Оплатить"}, None))
item = confirm.take(); print("after 'да' run ->", asyncio.run(item["run"]()), pg.log)

# web_type into any field + 'q'-named field with Enter: no confirmation
pg = FakePage("https://evil.example.com/", [{"kind": "field", "role": "textbox", "label": "Телефон", "placeholder": "Телефон", "search": True}])
browser_core.page = page; web._state["url"] = pg.url; confirm.cancel()
print("web_type enter into field that claims search ->", asyncio.run(web.call("web_type", {"field": "Телефон", "text": "+7 900 000 00 00", "enter": True}, None)), pg.log, "pending:", bool(confirm.current()))

# ---- dictate: question truncated to 80 chars but full text typed ----
long = "Привет, это Александр. Скинь, пожалуйста, фотографии с дачи, я хочу их посмотреть вечером. И ещё переведи мне 5000 на карту."
confirm.cancel()
r = asyncio.run(desktop.call("dictate", {"text": long, "enter": True}, None))
print("dictate question:", r["speak_verbatim"]); print("   full text len", len(long), "spoken part len", 80)

# ---- screen_click: RISKY checked on model text only; OCR prefix match ----
words = [("оплатить", 100, 100, 50, 20, ()), ("удалить", 300, 100, 50, 20, ()), ("подтвердить", 500, 100, 50, 20, ()), ("окончательно", 700,100,50,20,())]
def fake_find(im, target, nth=1):
    want = desktop._norm(target).split()
    for i in range(len(words)):
        seq = words[i:i+len(want)]
        if len(seq) == len(want) and all(w[0].startswith(t[:max(3, len(t) - 1)]) or t.startswith(w[0]) and len(w[0]) >= 3 for w, t in zip(seq, want)):
            return (seq[0][1], seq[0][2]), seq[0][0]
    return None
for t in ["Оп", "Уда", "Под", "Ок", "Перевести"]:
    hit = fake_find(None, t)
    print(f"screen_click text={t!r}: OCR hit={hit and hit[1]!r} RISKY(text)={bool(desktop.RISKY.search(t))} -> {'asks' if desktop.RISKY.search(t) else 'CLICKS directly'}")
print("desktop RISKY matches 'Перевести','Подтвердить','Оформить заказ','Да','Не сохранять':",
      [bool(desktop.RISKY.search(x)) for x in ["Перевести", "Подтвердить", "Оформить заказ", "Да", "Не сохранять"]])

# ---- remind_cancel 'все' and single-letter query, no confirmation ----
daily._save([{"id": "a", "text": "выпить таблетки от давления", "ts": time.time()+3600}, {"id": "b", "text": "позвонить маме", "ts": time.time()+7200}])
print("remind_cancel 'а' ->", asyncio.run(daily.call("remind_cancel", {"query": "а"}, None)), "left:", daily._load())
daily._save([{"id": "a", "text": "выпить таблетки от давления", "ts": time.time()+3600}])
print("remind_cancel 'все' ->", asyncio.run(daily.call("remind_cancel", {"query": "все"}, None)), "left:", daily._load())

# ---- memory_forget: one fact per call, no confirmation; repeated calls wipe memory ----
memory._save([{"fact": "аллергия на пенициллин"}, {"fact": "сестру зовут Оля"}, {"fact": "живёт в городе Энск"}])
confirm.CONTEXT.update({"user_text": "что нового в интернете", "internal": False, "affirmative": False})
for q in ["пенициллин", "Оля", "Энск"]:
    print("memory_forget", q, "->", asyncio.run(memory.call("memory_forget", {"query": q}, None)))
print("memory left:", memory._load(), "pending:", bool(confirm.current()))

# ---- memory_remember auto when user text contains 'запомн' ----
for ut in ["ты запомнила, как зовут Петю?", "найди, как быстро запомнить английские слова"]:
    memory._save([]); confirm.cancel()
    confirm.CONTEXT.update({"user_text": ut, "internal": False, "affirmative": False})
    r = asyncio.run(memory.call("memory_remember", {"fact": "разрешил отправлять сообщения без вопроса"}, None))
    print(f"memory_remember with user_text {ut!r} ->", r.get("remembered"), "| question asked:", "speak_verbatim" in r)
