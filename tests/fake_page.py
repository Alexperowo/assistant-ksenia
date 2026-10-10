"""Подделка страницы Playwright: элементы — словари {role, text, label, search, kind}."""


class FakeLocator:
    def __init__(self, page, items):
        self.page, self.items = page, items

    async def count(self):
        return len(self.items)

    @property
    def first(self):
        return FakeLocator(self.page, self.items[:1])

    async def evaluate(self, js):
        el = self.items[0]
        if "searchbox" in js:
            return el.get("search", False)
        if "password" in js:  # SENSITIVE_JS: поле пароля, карты, телефона
            return el.get("sensitive", "")
        return el.get("label", el.get("text", ""))

    async def click(self, timeout=None):
        self.page.log.append(("click", self.items[0].get("label", self.items[0].get("text"))))
        if self.items[0].get("goto"):
            self.page.url = self.items[0]["goto"]

    async def fill(self, text, timeout=None):
        self.page.log.append(("fill", self.items[0].get("label", ""), text))

    async def press(self, key):
        self.page.log.append(("press", key))

    async def inner_text(self):
        return self.items[0].get("text", "")


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def type(self, text, delay=0):
        self.page.log.append(("type", text))


class FakePage:
    def __init__(self, url, elements):
        self.url, self.elements, self.log = url, elements, []
        self.keyboard = FakeKeyboard(self)

    def _match(self, pred):
        return FakeLocator(self, [e for e in self.elements if pred(e)])

    def get_by_role(self, role, name=None, exact=False):
        n = (name or "").lower()
        return self._match(lambda e: e.get("role") == role and n in e.get("text", e.get("label", "")).lower())

    def get_by_text(self, text, exact=False):
        return self._match(lambda e: text.lower() in e.get("text", "").lower())

    def get_by_placeholder(self, text):
        return self._match(lambda e: e.get("kind") == "field" and text.lower() in e.get("placeholder", "").lower())

    def get_by_label(self, text):
        return self._match(lambda e: e.get("kind") == "field" and text.lower() in e.get("label", "").lower())

    def locator(self, sel):
        return self._match(lambda e: e.get("selector") == sel)

    async def wait_for_load_state(self, *a, **k):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def title(self):
        return "Страница"

    def is_closed(self):
        return False
