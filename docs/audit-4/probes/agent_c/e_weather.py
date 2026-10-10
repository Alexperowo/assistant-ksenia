import sys, asyncio
sys.path.insert(0, "/home/user/assistant-ksenia/core")
from tools import daily
class R:
    def __init__(self, status, body): self.status, self.body = status, body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self, content_type=None): return self.body
class S:
    def get(self, url, **k): return R(429, {"error": True, "reason": "Too many requests"})
print(asyncio.run(daily.call("weather", {"city": "Москва"}, S())))
