"""Инструменты зрения для Александра: описать экран, прочитать текст с экрана, буфер обмена, лупа.

- Снимок: spectacle (KWin ScreenShot2). Если монитор спит (DPMS off), KWin не рисует кадры и снимок
  зависает — поэтому монитор будим на время снимка и возвращаем обратно.
- Описание: Nex смотрит картинку во второй ячейке сервера (id_slot=1), чтобы не сбить кэш разговора.
- Чтение дословно: Tesseract (rus+eng) — точнее и быстрее нейросети для текста.
- Длинный текст не пересказывается: ядро зачитывает его как есть (поле speak_verbatim).
"""
import asyncio
import base64
import io
import json
import os
import subprocess
import time

import aiohttp
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
SHOT = os.path.join(ROOT, "..", "..", "logs", "screen.png")
BRAIN_KEY_FILE = "/home/user/Agents/Ksenia/brain/api_key"
BRAIN_URL = "http://127.0.0.1:18100"
READ_CHUNK = 1500

_reading = {"rest": ""}  # непрочитанный остаток длинного текста

SCHEMAS = [
    {"type": "function", "function": {
        "name": "screen_describe",
        "description": ("Посмотреть на экран и описать его. target=screen — то, что сейчас видно на мониторе "
                        "(если включена лупа — только увеличенный участок), window — активное окно целиком. "
                        "question — что именно нужно узнать (например «есть ли новые сообщения?», «где кнопка Отправить?»)."),
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "enum": ["screen", "window"]},
            "question": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "screen_read",
        "description": ("Прочитать вслух ДОСЛОВНО текст с экрана (активное окно или весь экран). "
                        "Ксения зачитает его сама, не пересказывай."),
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "enum": ["window", "screen"]}}}}},
    {"type": "function", "function": {
        "name": "clipboard_read",
        "description": "Прочитать вслух дословно: source=selection — выделенный текст, clipboard — скопированный.",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "enum": ["selection", "clipboard"]}}}}},
    {"type": "function", "function": {
        "name": "read_more",
        "description": "Продолжить чтение длинного текста с того места, где остановились.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "magnifier",
        "description": "Экранная лупа: zoom_in — увеличить, zoom_out — уменьшить.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["zoom_in", "zoom_out"]},
            "steps": {"type": "integer", "description": "сколько шагов (по умолчанию 1)"}},
            "required": ["action"]}}},
]


def _run(*args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _dpms_is_off():
    try:
        return "off" in _run("kscreen-doctor", "--dpms", "show", timeout=5).stdout
    except Exception:
        return False


def _capture(target):
    """Сделать снимок; при спящем мониторе — разбудить и вернуть в сон. Возвращает PIL.Image."""
    was_off = _dpms_is_off()
    if was_off:
        _run("kscreen-doctor", "--dpms", "on", timeout=5)
        time.sleep(1.5)
    try:
        if os.path.exists(SHOT):
            os.remove(SHOT)
        mode = "-a" if target == "window" else "-f"
        _run("spectacle", "-b", "-n", mode, "-o", SHOT, timeout=20)
        if not os.path.exists(SHOT):
            raise RuntimeError("снимок экрана не получился")
        im = Image.open(SHOT).convert("RGB")
        im.load()
        return im
    finally:
        if was_off:
            _run("kscreen-doctor", "--dpms", "off", timeout=5)


def _ocr(im):
    buf = io.BytesIO()
    im.save(buf, "PNG")
    p = subprocess.run(["tesseract", "stdin", "stdout", "-l", "rus+eng", "--psm", "3"],
                       input=buf.getvalue(), capture_output=True, timeout=60)
    text = p.stdout.decode("utf-8", "ignore")
    lines = [ln.strip() for ln in text.splitlines()]
    # склеиваем строки абзаца, выбрасываем мусор из одиночных символов
    out, para = [], []
    for ln in lines:
        if not ln:
            if para:
                out.append(" ".join(para))
                para = []
            continue
        if len(ln) <= 2 and not ln.isalnum():
            continue
        para.append(ln)
    if para:
        out.append(" ".join(para))
    return "\n".join(out).strip()


def _chunk(text):
    """Первые ~1500 символов по границе предложения, остаток — на «читай дальше»."""
    if len(text) <= READ_CHUNK:
        return text, ""
    cut = max(text.rfind(". ", 0, READ_CHUNK), text.rfind("\n", 0, READ_CHUNK))
    cut = cut + 1 if cut > READ_CHUNK // 2 else READ_CHUNK
    return text[:cut].strip(), text[cut:].strip()


def _verbatim(text, source):
    part, rest = _chunk(text)
    _reading["rest"] = rest
    return {"ok": True, "source": source, "speak_verbatim": part, "chars_total": len(text),
            "more_left": bool(rest),
            "note": "текст уже зачитывается дословно; не повторяй его, скажи максимум одну короткую фразу"}


async def _vision(im, question, session):
    im = im.copy()
    im.thumbnail((1600, 900))  # обзору хватает, а токенов картинки меньше — быстрее
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    q = question or "Опиши, что на экране: какие окна открыты, что в главном окне, есть ли что-то важное."
    body = {"id_slot": 1, "max_tokens": 400, "thinking_budget_tokens": 0, "messages": [
        {"role": "system", "content": ("Ты глаза слабовидящего человека. Отвечай по-русски, коротко (2–4 предложения), "
                                       "для прослушивания, без разметки. Только то, что действительно видно; "
                                       "если что-то не разобрать — так и скажи.")},
        {"role": "user", "content": [{"type": "text", "text": q},
                                     {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]}
    key = open(BRAIN_KEY_FILE).read().strip()
    async with session.post(BRAIN_URL + "/v1/chat/completions", json=body,
                            headers={"Authorization": "Bearer " + key},
                            timeout=aiohttp.ClientTimeout(total=120)) as r:
        d = await r.json()
    return (d["choices"][0]["message"].get("content") or "").strip()


async def call(name, args, session):
    if name == "screen_describe":
        im = await asyncio.to_thread(_capture, args.get("target", "screen"))
        text = await _vision(im, args.get("question"), session)
        return {"ok": True, "seen": text}
    if name == "screen_read":
        im = await asyncio.to_thread(_capture, args.get("target", "window"))
        text = await asyncio.to_thread(_ocr, im)
        if not text:
            return {"ok": False, "error": "текста на экране не нашла"}
        return _verbatim(text, "экран")
    if name == "clipboard_read":
        src = args.get("source", "selection")
        p = await asyncio.to_thread(_run, "wl-paste", "--no-newline", *(["--primary"] if src == "selection" else []))
        text = (p.stdout or "").strip()
        if not text:
            return {"ok": False, "error": "выделенного текста нет" if src == "selection" else "буфер обмена пуст"}
        return _verbatim(text, "выделенное" if src == "selection" else "буфер обмена")
    if name == "read_more":
        if not _reading["rest"]:
            return {"ok": False, "error": "читать больше нечего"}
        return _verbatim(_reading["rest"], "продолжение")
    if name == "magnifier":
        action = "view_zoom_in" if args.get("action") == "zoom_in" else "view_zoom_out"
        for _ in range(max(1, min(int(args.get("steps") or 1), 5))):
            await asyncio.to_thread(_run, "qdbus6", "org.kde.kglobalaccel", "/component/kwin",
                                    "org.kde.kglobalaccel.Component.invokeShortcut", action)
            await asyncio.sleep(0.15)
        return {"ok": True, "action": args.get("action")}
    return {"ok": False, "error": f"неизвестный инструмент {name}"}
