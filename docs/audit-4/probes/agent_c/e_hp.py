import sys, asyncio, aiohttp
sys.path.insert(0, "/home/user/assistant-ksenia/core")
from tools import headphones
headphones.VOICE_IN = "http://127.0.0.1:1"   # voice-in down
async def main():
    async with aiohttp.ClientSession() as s:
        try:
            await headphones.call("headphones", {"action": "fix"}, s)
        except Exception as e:
            print(f"сбой инструмента: {e!r}"[:300])
asyncio.run(main())
