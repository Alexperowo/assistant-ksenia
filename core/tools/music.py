"""Инструмент «музыка»: интернет-радио (radio-browser.info) через постоянный плеер mpv.

YouTube и SoundCloud из сети Александра недоступны, radio-browser работает без входа.
Плеер управляется через IPC-сокет mpv; громкость приглушается, пока Ксения слушает и говорит.
"""
import asyncio
import json
import os
import random
import urllib.parse

import aiohttp

SOCK = f"/run/user/{os.getuid()}/ksenia-mpv.sock"
API = "https://de1.api.radio-browser.info/json"
NORMAL_VOLUME = 70
DUCK_VOLUME = 20

_state = {"station": None, "volume": NORMAL_VOLUME, "ducked": False, "last_query": None, "last_results": []}
_proc = None

SCHEMAS = [
    {"type": "function", "function": {
        "name": "music_play",
        "description": ("Включить интернет-радио. query — жанр или настроение одним-двумя словами по-английски "
                        "(rock, jazz, classical, lofi, chillout, dance, pop, metal, blues, ambient, electronic, "
                        "russian rock, russian pop, 80s, retro, news) или название станции. "
                        "Если просят «что-нибудь бодрое/спокойное» — выбери жанр сама. "
                        "Конкретные песни по названию сейчас недоступны — честно скажи об этом и предложи радио."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "russian": {"type": "boolean", "description": "российские станции (по умолчанию да; false — если просят зарубежное)"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "music_control",
        "description": "Управление музыкой: pause, resume, stop, volume_up, volume_down, next (другая станция того же жанра).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["pause", "resume", "stop", "volume_up", "volume_down", "next"]}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "music_status",
        "description": "Что сейчас играет (станция и, если есть, название трека).",
        "parameters": {"type": "object", "properties": {}}}},
]


async def _ensure_mpv():
    global _proc
    if _proc and _proc.returncode is None and os.path.exists(SOCK):
        return
    if os.path.exists(SOCK):
        os.remove(SOCK)
    _proc = await asyncio.create_subprocess_exec(
        "mpv", "--no-video", "--idle=yes", "--no-terminal", f"--input-ipc-server={SOCK}",
        f"--volume={_state['volume']}", "--cache=yes", "--demuxer-max-bytes=2MiB",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    for _ in range(50):
        if os.path.exists(SOCK):
            return
        await asyncio.sleep(0.05)


async def _ipc(*command):
    if not os.path.exists(SOCK):
        return None
    try:
        r, w = await asyncio.open_unix_connection(SOCK)
        w.write((json.dumps({"command": list(command)}) + "\n").encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout=2)
        w.close()
        return json.loads(line)
    except Exception:
        return None


async def _search(session, query: str, russian: bool):
    q = query.strip().lower()
    params_list = []
    base = {"order": "clickcount", "reverse": "true", "limit": "15", "hidebroken": "true"}
    if russian:
        params_list.append({**base, "tag": q, "countrycode": "RU"})
    params_list.append({**base, "tag": q})
    params_list.append({**base, "name": q})
    params_list.append({**base, "tagList": ",".join(q.split())})
    for p in params_list:
        url = f"{API}/stations/search?{urllib.parse.urlencode(p)}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                               headers={"User-Agent": "Ksenia/0.1"}) as r:
            res = [s for s in await r.json() if s.get("url_resolved")]
        if res:
            return res
    return []


async def _play_station(st):
    await _ensure_mpv()
    await _ipc("loadfile", st["url_resolved"], "replace")
    await _ipc("set_property", "pause", False)
    _state["station"] = st["name"].strip()
    await _apply_volume()


async def _apply_volume():
    vol = DUCK_VOLUME if _state["ducked"] else _state["volume"]
    await _ipc("set_property", "volume", min(vol, _state["volume"]))


async def duck(on: bool):
    """Приглушить музыку, пока Ксения слушает или говорит."""
    if _state["ducked"] == on:
        return
    _state["ducked"] = on
    if _state["station"]:
        await _apply_volume()


async def call(name: str, args: dict, session) -> dict:
    if name == "music_play":
        query = args.get("query") or "pop"
        res = await _search(session, query, args.get("russian", True) is not False)
        if not res:
            return {"ok": False, "error": f"не нашла радиостанций по запросу «{query}»"}
        _state["last_query"], _state["last_results"] = query, res
        # для музыки избегаем разговорных станций (новости/ток-шоу), если только их не просили
        if not any(w in query.lower() for w in ("news", "talk", "новост")):
            music_only = [x for x in res if not any(t in (x.get("tags") or "").lower() for t in ("news", "talk"))]
            res = music_only or res
            _state["last_results"] = res
        st = random.choice(res[:5])
        await _play_station(st)
        return {"ok": True, "station": _state["station"], "genre_tags": st.get("tags", "")[:80],
                "country": st.get("country", "")}
    if name == "music_control":
        a = args.get("action")
        if a == "pause":
            await _ipc("set_property", "pause", True)
        elif a == "resume":
            await _ipc("set_property", "pause", False)
        elif a == "stop":
            await _ipc("stop")
            _state["station"] = None
        elif a in ("volume_up", "volume_down"):
            _state["volume"] = max(10, min(100, _state["volume"] + (15 if a == "volume_up" else -15)))
            await _apply_volume()
        elif a == "next":
            others = [s for s in _state["last_results"] if s["name"].strip() != _state["station"]]
            if not others:
                return {"ok": False, "error": "других станций этого жанра нет"}
            await _play_station(random.choice(others[:8]))
        return {"ok": True, "action": a, "station": _state["station"], "volume": _state["volume"]}
    if name == "music_status":
        title = (await _ipc("get_property", "media-title") or {}).get("data")
        paused = (await _ipc("get_property", "pause") or {}).get("data")
        return {"ok": True, "station": _state["station"], "track": title, "paused": paused,
                "volume": _state["volume"]}
    return {"ok": False, "error": f"неизвестный инструмент {name}"}
