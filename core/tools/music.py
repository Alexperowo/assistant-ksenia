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

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "secrets", "yandex_music_token")
_ym = None  # клиент Яндекс Музыки (ленивая инициализация)

BOOKS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "books.json")
# текущая книга, последние варианты поиска и с какой главы начат плейлист mpv (его позиция 0 = эта глава)
_book = {"id": None, "title": None, "search": [], "first": 0}

_state = {"playlist": [],
          "station": None, "volume": NORMAL_VOLUME, "ducked": False, "last_query": None, "last_results": []}
_proc = None
_play = {"gen": 0, "task": None}
_ipc_ids = [0]  # номер текущего включения и фоновая дозагрузка его очереди


def _new_playback():
    """Новое включение: прошлая дозагрузка очереди больше не должна дописывать свои треки в mpv."""
    _play["gen"] += 1
    if _play["task"] and not _play["task"].done():
        _play["task"].cancel()
    _play["task"] = None
    return _play["gen"]


def _start_append(gen, items, label_of):
    """Дописать треки в очередь mpv в фоне (прямые ссылки получаются по одной, это медленно)."""
    async def append_rest():
        for t, label in items:
            try:
                link = await asyncio.to_thread(_direct_link, t)
            except Exception:
                continue
            if _play["gen"] != gen:  # пока ждали ссылку, включили другое
                return
            await _ipc("loadfile", link, "append")
            _state["playlist"].append(label_of(t, label))
    _play["task"] = asyncio.create_task(append_rest())

SCHEMAS = [
    {"type": "function", "function": {
        "name": "music_play",
        "description": ("Включить интернет-радио. query — жанр или настроение одним-двумя словами по-английски "
                        "(rock, jazz, classical, lofi, chillout, dance, pop, metal, blues, ambient, electronic, "
                        "russian rock, russian pop, 80s, retro, news) или название станции. "
                        "Если просят «что-нибудь бодрое/спокойное» — выбери жанр сама. "
                        "Для конкретных песен, исполнителей и альбомов используй music_song (Яндекс Музыка)."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "russian": {"type": "boolean", "description": "российские станции (по умолчанию да; false — если просят зарубежное)"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "music_song",
        "description": ("Яндекс Музыка: включить конкретную песню, исполнителя или альбом. "
                        "kind=track — песня (дальше играют похожие), artist — популярное у исполнителя, album — альбом целиком."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "например «Кино Группа крови», «Земфира», «Сплин Гранатовый альбом»"},
            "kind": {"type": "string", "enum": ["track", "artist", "album"]}},
            "required": ["query", "kind"]}}},
    {"type": "function", "function": {
        "name": "music_wave",
        "description": "Яндекс Музыка: «Моя волна» — персональный поток под вкус Александра, или его любимые треки (liked=true).",
        "parameters": {"type": "object", "properties": {"liked": {"type": "boolean"}}}}},
    {"type": "function", "function": {
        "name": "audiobook",
        "description": ("Яндекс Музыка: аудиокниги и подкасты. action=search — найти варианты (разные чтецы/версии), "
                        "action=play — включить вариант (option — номер из поиска, по умолчанию 1), "
                        "action=continue — продолжить последнюю книгу с того же места. Если Александр называет чтеца — action=play с reader."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["search", "play", "continue"]},
            "query": {"type": "string", "description": "название книги или автор"},
            "option": {"type": "integer"},
            "reader": {"type": "string", "description": "фамилия чтеца, если Александр её назвал (например «Смехов»)"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "music_control",
        "description": "Управление плеером (музыка и книги): pause, resume, stop, volume_up, volume_down, next/previous (трек или глава), faster/slower/normal_speed (скорость чтения книги).",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["pause", "resume", "stop", "volume_up", "volume_down", "next", "previous", "faster", "slower", "normal_speed"]}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "music_status",
        "description": "Что сейчас играет (станция/трек/глава книги). ВСЕГДА вызывай, когда спрашивают, что играет, — не отвечай по памяти.",
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
    """Команда mpv через IPC-сокет -> ответ (dict) или None.

    mpv шлёт в тот же сокет события ({"event": ...}: смена трека, метаданные радио) — они могут прийти
    раньше ответа, поэтому ответ ищем по request_id."""
    if not os.path.exists(SOCK):
        return None
    _ipc_ids[0] += 1
    rid = _ipc_ids[0]
    w = None
    try:
        r, w = await asyncio.open_unix_connection(SOCK)
        w.write((json.dumps({"command": list(command), "request_id": rid}) + "\n").encode())
        await w.drain()

        async def reply():
            while True:
                line = await r.readline()
                if not line:
                    return None
                msg = json.loads(line)
                if isinstance(msg, dict) and "event" not in msg and msg.get("request_id", rid) == rid:
                    return msg

        return await asyncio.wait_for(reply(), timeout=2)
    except Exception:
        return None
    finally:
        if w:
            w.close()


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
            res = _safe_stations(await r.json(content_type=None))
        if res:
            return res
    return []


def _safe_stations(raw):
    """Станции radio-browser — недоверенные данные (их добавляет кто угодно). Оставляем только http(s)-потоки:
    mpv открыл бы и file://, av://, lavfi:// и прочие протоколы. Имя — короткое, одной строкой."""
    out = []
    for st in raw if isinstance(raw, list) else []:
        if not isinstance(st, dict):
            continue
        url = str(st.get("url_resolved") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        name = " ".join(str(st.get("name") or "").split())[:80] or "станция без названия"
        out.append({**st, "url_resolved": url, "name": name, "tags": str(st.get("tags") or "")[:200],
                    "country": str(st.get("country") or "")[:60]})
    return out


def _music_only(stations, query):
    """Для музыки избегаем разговорных станций (новости/ток-шоу), если только их не просили."""
    if any(w in query.lower() for w in ("news", "talk", "новост")):
        return stations
    songs = [x for x in stations if not any(t in x.get("tags", "").lower() for t in ("news", "talk"))]
    return songs or stations


def _clean_title(title):
    """Название трека из метаданных станции — недоверенный текст: служебный мусор (JSON, адреса, коды) отбрасываем."""
    if not isinstance(title, str):
        return None
    t = " ".join(title.split())
    if not t or len(t) > 150 or any(c in t for c in "{}<>\\") or t.startswith(("http", "/")) or \
            t.lower().endswith((".mp3", ".aac", ".m3u8", ".pls")):
        return None
    return t


def _ym_client():
    global _ym
    if _ym is None:
        import logging
        logging.getLogger("yandex_music").setLevel(logging.ERROR)
        from yandex_music import Client
        _ym = Client(open(TOKEN_FILE).read().strip(), report_unknown_fields=False).init()
    return _ym


def _track_name(t):
    return f"{t.title} — {', '.join(a.name for a in (t.artists or []))}"


def _direct_link(t):
    info = t.get_download_info(get_direct_links=True)
    mp3 = [i for i in info if i.codec == "mp3"] or info
    return max(mp3, key=lambda x: x.bitrate_in_kbps).direct_link


def _ym_collect(kind, query=None, liked=False, limit=12):
    """Собрать список треков (синхронно, в отдельном потоке)."""
    c = _ym_client()
    if kind == "wave":
        if liked:
            ids = [t.id for t in c.users_likes_tracks()[:200]]
            random.shuffle(ids)
            tracks = c.tracks(ids[:limit])
        else:
            res = c.rotor_station_tracks("user:onyourwave")
            tracks = [s.track for s in res.sequence][:limit]
        return tracks, ("любимые треки" if liked else "Моя волна (говори: «включаю Мою волну»)")
    r = c.search(query, type_=kind)
    if kind == "track":
        if not r.tracks or not r.tracks.results:
            return [], None
        first = r.tracks.results[0]
        similar = []
        try:
            sim = c.tracks_similar(first.id)
            similar = sim.similar_tracks[:limit - 1] if sim else []
        except Exception:
            pass
        return [first] + similar, _track_name(first)
    if kind == "artist":
        if not r.artists or not r.artists.results:
            return [], None
        art = r.artists.results[0]
        tr = c.artists_tracks(art.id, page_size=limit)
        return list(tr.tracks if tr else []), art.name
    if kind == "album":
        if not r.albums or not r.albums.results:
            return [], None
        alb = c.albums_with_tracks(r.albums.results[0].id)
        tracks = [t for vol in (alb.volumes or []) for t in vol][:30]
        return tracks, f"{alb.title} — {', '.join(a.name for a in (alb.artists or []))}"
    return [], None


async def _play_tracks(tracks, label):
    """Первый трек — сразу, остальные дописываем в очередь mpv в фоне (ссылки получаются по одной)."""
    gen = _new_playback()
    await save_book_position()
    _book["id"] = None
    await _ensure_mpv()
    first_link = await asyncio.to_thread(_direct_link, tracks[0])
    await _ipc("loadfile", first_link, "replace")
    await _ipc("set_property", "pause", False)
    _state["playlist"] = [_track_name(tracks[0])]
    _state["station"] = f"Яндекс Музыка: {label}"
    _state["paused"] = False
    await _apply_volume()
    _start_append(gen, [(t, None) for t in tracks[1:]], lambda t, _: _track_name(t))


def _books_load():
    try:
        return json.load(open(BOOKS_FILE, encoding="utf-8"))
    except Exception:
        return {"last": None, "positions": {}}


def _books_save(db):
    os.makedirs(os.path.dirname(BOOKS_FILE), exist_ok=True)
    tmp = BOOKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, BOOKS_FILE)


async def save_book_position():
    """Запомнить главу и секунду текущей книги (вызывается при паузе/стопе/смене и периодически)."""
    if not _book["id"]:
        return
    pos = (await _ipc("get_property", "playlist-pos") or {}).get("data")
    tpos = (await _ipc("get_property", "time-pos") or {}).get("data")
    if not isinstance(pos, int) or pos < 0:
        return
    db = _books_load()
    db["last"] = _book["id"]
    # playlist-pos считается от главы, с которой включили, а не от начала книги
    db["positions"][str(_book["id"])] = {"title": _book["title"], "chapter": _book["first"] + pos,
                                          "time": float(tpos or 0), "saved": int(__import__("time").time())}
    _books_save(db)


def _ym_book_search(query):
    c = _ym_client()
    r = c.search(query, type_="podcast")
    res = [a for a in (r.podcasts.results if r.podcasts else [])][:10]
    books = [a for a in res if a.type == "audiobook"] or res
    return [{"id": a.id, "title": a.title, "kind": a.type or "podcast", "chapters": a.track_count,
             "readers": ", ".join(x.name for x in (a.artists or []))[:80]} for a in books[:8]]


def _ym_book_tracks(album_id):
    c = _ym_client()
    alb = c.albums_with_tracks(album_id)
    return [t for vol in (alb.volumes or []) for t in vol], alb.title


async def _play_book(album_id, chapter=0, seconds=0.0):
    tracks, title = await asyncio.to_thread(_ym_book_tracks, album_id)
    if not tracks:
        return {"ok": False, "error": "в книге нет глав"}
    gen = _new_playback()
    await save_book_position()
    chapter = max(0, min(chapter, len(tracks) - 1))
    _book["id"], _book["title"], _book["first"] = album_id, title, chapter
    await _ensure_mpv()
    link = await asyncio.to_thread(_direct_link, tracks[chapter])
    await _ipc("loadfile", link, "replace", -1, f"start={seconds:.1f}" if seconds > 1 else "start=0")
    await _ipc("set_property", "pause", False)
    _state["playlist"] = [None] * chapter + [f"{title}, глава {chapter + 1}"]
    _state["station"] = f"Аудиокнига: {title}"
    _state["paused"] = False
    await _apply_volume()

    _start_append(gen, [(t, i) for i, t in enumerate(tracks[chapter + 1:chapter + 40], start=chapter + 1)],
                  lambda t, i: f"{title}, глава {i + 1}")
    return {"ok": True, "book": title, "chapter": chapter + 1, "chapters_total": len(tracks),
            "from_minute": round(seconds / 60, 1)}


async def book_autosave_loop():
    while True:
        await asyncio.sleep(30)
        try:
            await save_book_position()
        except Exception:
            pass


async def _play_station(st):
    _new_playback()
    _state["playlist"] = []
    await _ensure_mpv()
    await _ipc("loadfile", st["url_resolved"], "replace")
    await _ipc("set_property", "pause", False)
    _state["station"] = st["name"]
    _state["paused"] = False
    await _apply_volume()


async def _apply_volume():
    vol = DUCK_VOLUME if _state["ducked"] else _state["volume"]
    await _ipc("set_property", "volume", min(vol, _state["volume"]))


_duck_depth = 0  # вложенность: разговор + реплика внутри него не должны вернуть громкость раньше времени


def playing() -> bool:
    """Музыка или книга играет (не выключена и не на паузе)."""
    return bool(_state["station"]) and not _state.get("paused")


def status():
    """"playing" | "paused" | None — для кнопок наушников."""
    if not _state["station"]:
        return None
    return "paused" if _state.get("paused") else "playing"


def title() -> str:
    """Что включено: станция, альбом или книга — подпись плеера «Ксения» в KDE."""
    return _state["station"] or ""


async def duck(on: bool):
    """Приглушить музыку, пока Ксения слушает или говорит (со счётчиком вложенности)."""
    global _duck_depth
    _duck_depth = _duck_depth + 1 if on else max(0, _duck_depth - 1)
    want = _duck_depth > 0
    if _state["ducked"] == want:
        return
    _state["ducked"] = want
    if _state["station"]:
        await _apply_volume()


async def call(name: str, args: dict, session) -> dict:
    if name == "music_play":
        query = args.get("query") or "pop"
        res = await _search(session, query, args.get("russian", True) is not False)
        if not res:
            return {"ok": False, "error": f"не нашла радиостанций по запросу «{query}»"}
        res = _music_only(res, query)
        _state["last_query"], _state["last_results"] = query, res
        st = random.choice(res[:5])
        await _play_station(st)
        return {"ok": True, "station": _state["station"], "genre_tags": st["tags"][:80], "country": st["country"]}
    if name == "audiobook":
        if not os.path.exists(TOKEN_FILE):
            return {"ok": False, "error": "Яндекс Музыка не подключена (нет ключа)"}
        a = args.get("action", "search")
        try:
            if a == "continue":
                await save_book_position()  # книга играет прямо сейчас — продолжаем с текущего места, а не с автосохранения
                db = _books_load()
                if not db.get("last"):
                    return {"ok": False, "error": "ещё нет начатых книг"}
                p = db["positions"][str(db["last"])]
                return await _play_book(int(db["last"]), p["chapter"], max(0.0, p["time"] - 5))
            if a == "search" or not _book["search"] or (args.get("query") and not args.get("reader")):
                if not args.get("query"):
                    return {"ok": False, "error": "не сказано, какую книгу искать"}
                _book["search"] = await asyncio.to_thread(_ym_book_search, args["query"])
                if a == "search":
                    return {"ok": True, "options": _book["search"]}
            if not _book["search"]:
                return {"ok": False, "error": "ничего не нашла"}
            reader = (args.get("reader") or "").strip().lower()[:6]  # «Смехов»/«Смеховым» -> «смехов»
            if reader:
                match = [i for i, b in enumerate(_book["search"]) if reader in b["readers"].lower()]
                if not match and _book["title"] is None and args.get("query"):
                    pass
                if not match:
                    extra = await asyncio.to_thread(_ym_book_search, f"{args.get('query') or ''} {args['reader']}".strip())
                    _book["search"] = extra + [b for b in _book["search"] if b["id"] not in {e["id"] for e in extra}]
                    match = [i for i, b in enumerate(_book["search"]) if reader in b["readers"].lower()]
                if not match:
                    return {"ok": False, "error": f"версии с чтецом «{args['reader']}» не нашла",
                            "options": _book["search"][:5]}
                args["option"] = match[0] + 1
            opt = max(1, min(int(args.get("option") or 1), len(_book["search"]))) - 1
            book_id = _book["search"][opt]["id"]
            saved = _books_load()["positions"].get(str(book_id))
            if saved:  # эту книгу уже слушали — продолжаем с того же места
                return await _play_book(book_id, saved["chapter"], max(0.0, saved["time"] - 5))
            return await _play_book(book_id)
        except Exception as e:
            return {"ok": False, "error": f"Яндекс Музыка не ответила: {e}"}
    if name in ("music_song", "music_wave"):
        if not os.path.exists(TOKEN_FILE):
            return {"ok": False, "error": "Яндекс Музыка не подключена (нет ключа)"}
        try:
            if name == "music_song":
                tracks, label = await asyncio.to_thread(_ym_collect, args.get("kind", "track"), args.get("query", ""))
            else:
                tracks, label = await asyncio.to_thread(_ym_collect, "wave", None, bool(args.get("liked")))
        except Exception as e:
            return {"ok": False, "error": f"Яндекс Музыка не ответила: {e}"}
        if not tracks:
            return {"ok": False, "error": f"ничего не нашла в Яндекс Музыке по запросу «{args.get('query', '')}»"}
        await _play_tracks(tracks, label)
        return {"ok": True, "playing": _track_name(tracks[0]), "source": _state["station"],
                "queue_len": len(tracks)}
    if name == "music_control":
        a = args.get("action")
        if a in ("pause", "stop"):
            await save_book_position()
        if a == "pause":
            await _ipc("set_property", "pause", True)
            _state["paused"] = True
        elif a == "resume":
            await _ipc("set_property", "pause", False)
            _state["paused"] = False
        elif a == "stop":
            _new_playback()
            await _ipc("stop")
            await _ipc("playlist-clear")
            _state["station"] = None
            _state["playlist"] = []
            _book["id"] = None
        elif a in ("volume_up", "volume_down"):
            _state["volume"] = max(10, min(100, _state["volume"] + (15 if a == "volume_up" else -15)))
            await _apply_volume()
        elif a in ("faster", "slower", "normal_speed"):
            cur = (await _ipc("get_property", "speed") or {}).get("data") or 1.0
            new = 1.0 if a == "normal_speed" else max(0.7, min(2.0, cur + (0.15 if a == "faster" else -0.15)))
            await _ipc("set_property", "speed", round(new, 2))
            return {"ok": True, "action": a, "speed": round(new, 2)}
        elif a in ("next", "previous") and _state["playlist"]:
            await _ipc("playlist-next" if a == "next" else "playlist-prev", "force")
        elif a == "previous":
            return {"ok": False, "error": "у радио нет предыдущего трека"}
        elif a == "next":
            others = [s for s in _state["last_results"] if s["name"] != _state["station"]]
            if not others:
                return {"ok": False, "error": "других станций этого жанра нет"}
            await _play_station(random.choice(others[:8]))
        return {"ok": True, "action": a, "station": _state["station"], "volume": _state["volume"]}
    if name == "music_status":
        title = _clean_title((await _ipc("get_property", "media-title") or {}).get("data"))
        if _state["playlist"]:
            pos = (await _ipc("get_property", "playlist-pos") or {}).get("data")
            offset = sum(1 for x in _state["playlist"] if x is None)  # книга начата не с первой главы
            if isinstance(pos, int) and 0 <= pos + offset < len(_state["playlist"]):
                title = _state["playlist"][pos + offset]
        paused = (await _ipc("get_property", "pause") or {}).get("data")
        return {"ok": True, "station": _state["station"], "track": title, "paused": paused,
                "volume": _state["volume"]}
    return {"ok": False, "error": f"неизвестный инструмент {name}"}
