import sys, asyncio
sys.path.insert(0, "/home/user/assistant-ksenia/core")
from tools import music
launched, ipc = [], []
music._yt_find = lambda q, video=False: ("Some title", 240, "https://www.youtube.com/watch?v=x", "https://rr1.googlevideo.com/a")
async def nop(*a, **k): return None
async def fake_ipc(*a): ipc.append(a); return {}
music._ensure_mpv = nop; music._ipc = fake_ipc; music.save_book_position = nop
async def fake_exec(*a, **k): launched.append(a); return type("P", (), {"returncode": None})()
music.asyncio.create_subprocess_exec = fake_exec
for flag in (True, False):
    music.CONFIG_VIDEO["on"] = flag
    launched.clear(); ipc.clear()
    r = asyncio.run(music.call("youtube", {"query": "лекция", "video": True}, None))
    print(f"CONFIG_VIDEO on={flag}: video=True ->", r, "| fullscreen mpv launched:", bool(launched), "| audio loadfile:", any(x[0] == 'loadfile' for x in ipc))
