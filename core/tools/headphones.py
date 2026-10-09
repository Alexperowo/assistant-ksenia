"""Наушники голосом: режим музыки (обычный Bluetooth, лучший звук) / режим разговора (LE Audio, живой разговор
с перебиваниями), проверить и починить канал звука. Вся работа с Bluetooth — в слухе (voice-in/headset.py)."""
import aiohttp

VOICE_IN = "http://127.0.0.1:18120"

SCHEMAS = [
    {"type": "function", "function": {
        "name": "headphones",
        "description": ("Наушники. status — какой режим и подключены ли; fix — проверить и починить звук (переподключить); "
                        "music_mode — режим музыки: лучший звук, но слушаю тебя только после сигнала; "
                        "talk_mode — режим разговора: слышу всегда, можно перебивать, звук чуть проще. "
                        "Переключение занимает 10–20 секунд, звук на это время пропадает — предупреди."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["status", "fix", "music_mode", "talk_mode"]}},
            "required": ["action"]}}},
]
TIMEOUTS = {"headphones": 90}


async def call(name, args, session):
    a = args.get("action", "status")
    timeout = aiohttp.ClientTimeout(total=85)
    if a == "status":
        async with session.get(VOICE_IN + "/headset", timeout=timeout) as r:
            st = await r.json(content_type=None)
        meaning = {"talk": "режим разговора (LE Audio)", "music": "режим музыки (обычный Bluetooth)"}.get(st.get("mode"))
        return {"ok": True, "connected": st.get("connected"), "mode": meaning or "неизвестно", "raw": st}
    path = {"fix": "/headset/check", "music_mode": "/headset/mode/music", "talk_mode": "/headset/mode/talk"}.get(a)
    if not path:
        return {"ok": False, "error": f"неизвестное действие {a}"}
    async with session.post(VOICE_IN + path, timeout=timeout) as r:
        return await r.json(content_type=None)
