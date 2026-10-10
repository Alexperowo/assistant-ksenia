"""Файлы голосом: найти, что нового в загрузках, открыть, прочитать вслух, разложить по папкам, убрать в корзину.

Александр редко подходит к компьютеру и не помнит, где что лежит («где агент положил, там и лежит»):
поиск — по всей домашней папке, по смыслу названия; свои файлы Ксения кладёт в «Документы/Ксения».
Перенос и корзина — только после «да» (ядро); удаления насовсем нет — только в корзину, откуда можно вернуть.
"""
import asyncio
import os
import re
import shutil
import subprocess
import time
import zipfile

from tools import confirm, screen

HOME = os.path.expanduser("~")
SKIP_DIRS = {".cache", ".local", ".config", ".venv", "venv", "node_modules", "__pycache__", ".git", "Models", "backend",
             "snap", ".mozilla", ".var", "site-packages", ".npm", ".cargo", ".rustup", "Agents"}
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".ini", ".conf", ".py", ".sh", ".html", ".xml", ".srt"}
KIND_EXT = {"документ": TEXT_EXT | {".pdf", ".docx", ".odt", ".doc", ".rtf", ".xlsx", ".ods"},
            "фото": {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp"},
            "музыка": {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus"},
            "видео": {".mp4", ".mkv", ".avi", ".webm", ".mov"},
            "архив": {".zip", ".rar", ".7z", ".tar", ".gz"}}
_state = {"last": []}

SCHEMAS = [
    {"type": "function", "function": {
        "name": "files",
        "description": ("Файлы на компьютере. find — найти по словам из названия (query; kind: документ/фото/музыка/видео/"
                        "архив); recent — что нового в загрузках и документах; open — открыть (n — номер из последнего "
                        "списка); read — прочитать вслух документ (txt, pdf, docx, odt); move — переложить в папку "
                        "(to: документы, фото, музыка, видео, ксения; спросит «да»); trash — в корзину (спросит «да»)."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["find", "recent", "open", "read", "move", "trash"]},
            "query": {"type": "string"}, "kind": {"type": "string"}, "n": {"type": "integer"},
            "to": {"type": "string"}}, "required": ["action"]}}},
]
TIMEOUTS = {"files": 60}


def _dir(xdg, fallback):
    try:
        p = subprocess.run(["xdg-user-dir", xdg], capture_output=True, text=True, timeout=3).stdout.strip()
        return p or os.path.join(HOME, fallback)
    except (OSError, subprocess.SubprocessError):
        return os.path.join(HOME, fallback)


FOLDERS = {"документы": ("DOCUMENTS", "Documents"), "фото": ("PICTURES", "Pictures"), "музыка": ("MUSIC", "Music"),
           "видео": ("VIDEOS", "Videos"), "загрузки": ("DOWNLOAD", "Downloads")}


def folder(name):
    name = (name or "").lower().strip()
    if name.startswith("ксени"):
        p = os.path.join(_dir("DOCUMENTS", "Documents"), "Ксения")
        os.makedirs(p, exist_ok=True)
        return p
    for key, (xdg, fb) in FOLDERS.items():
        if name.startswith(key[:4]):
            return _dir(xdg, fb)
    return None


def _when(ts):
    d = time.time() - ts
    if d < 3600:
        return "только что"
    if d < 86400:
        return "сегодня" if time.localtime(ts).tm_yday == time.localtime().tm_yday else "вчера"
    if d < 2 * 86400:
        return "вчера"
    return time.strftime("%d.%m", time.localtime(ts))


def _norm(s):
    return re.sub(r"[^\w]+", " ", s.lower().replace("ё", "е")).split()


def search(query="", kind=None, roots=None, limit=10, max_files=200000, time_limit=4.0, any_word=False):
    """Файлы, в названии которых есть все слова запроса (по началу слова), новые — первыми.
    Ничего не нашлось по всем словам — по любому из них («Ксения ksenia» -> ksenia.desktop)."""
    words = _norm(query)
    exts = KIND_EXT.get((kind or "").lower())
    found, seen, t0 = [], 0, time.time()
    for root in roots or [HOME]:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
            for fn in filenames:
                seen += 1
                if fn.startswith("."):
                    continue
                stem, ext = os.path.splitext(fn)
                if exts and ext.lower() not in exts:
                    continue
                name_words = _norm(stem)
                hit = [any(w2.startswith(w[:5]) for w2 in name_words) for w in words]
                if words and not (any(hit) if any_word else all(hit)):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    found.append((os.path.getmtime(p), p))
                except OSError:
                    pass
            if seen > max_files or time.time() - t0 > time_limit:
                break
    found.sort(reverse=True)
    if not found and len(words) > 1 and not any_word:
        return search(query, kind, roots, limit, max_files, time_limit, any_word=True)
    return [p for _, p in found[:limit]]


def _describe(paths):
    _state["last"] = paths
    out = []
    for i, p in enumerate(paths, 1):
        try:
            ts, size = os.path.getmtime(p), os.path.getsize(p)
        except OSError:
            continue
        where = os.path.basename(os.path.dirname(p)) or "~"
        out.append({"n": i, "name": os.path.basename(p), "folder": where, "when": _when(ts),
                    "size_kb": max(1, size // 1024)})
    return out


def _pick(n):
    last = _state["last"]
    if not last:
        return None, {"ok": False, "error": "сначала найди файл (find или recent)"}
    n = n or 1
    if not 1 <= n <= len(last):
        return None, {"ok": False, "error": f"в последнем списке {len(last)} файлов"}
    return last[n - 1], None


def extract_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(400000)
    if ext == ".pdf":
        return subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", path, "-"], capture_output=True, text=True,
                              timeout=40).stdout
    if ext in (".docx", ".odt"):
        part = "word/document.xml" if ext == ".docx" else "content.xml"
        with zipfile.ZipFile(path) as z:
            xml = z.read(part).decode("utf-8", "replace")
        xml = re.sub(r"</w:p>|</text:p>|</text:h>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        return re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", re.sub(r"&amp;", "&", re.sub(r"&quot;", '"', text))))
    if ext in (".doc", ".rtf", ".xlsx", ".ods"):
        out = os.path.join("/tmp", f"ksenia-conv-{os.getpid()}")
        os.makedirs(out, exist_ok=True)
        subprocess.run(["libreoffice", "--headless", "--convert-to", "txt:Text", "--outdir", out, path],
                       capture_output=True, timeout=60)
        txt = os.path.join(out, os.path.splitext(os.path.basename(path))[0] + ".txt")
        if os.path.exists(txt):
            with open(txt, encoding="utf-8", errors="replace") as f:
                return f.read(400000)
    return None


async def _move(src, dst_dir):
    dst = os.path.join(dst_dir, os.path.basename(src))
    base, ext = os.path.splitext(dst)
    n = 2
    while os.path.exists(dst):  # «(2)», «(3)»…: имя с минутами совпадало при двух переносах за минуту (аудит Fable)
        dst = f"{base} ({n}){ext}"
        n += 1
    await asyncio.to_thread(shutil.move, src, dst)
    return {"ok": True, "moved_to": os.path.basename(dst_dir), "name": os.path.basename(dst)}


async def _trash(path):
    p = await asyncio.create_subprocess_exec("gio", "trash", path, stdout=asyncio.subprocess.DEVNULL,
                                             stderr=asyncio.subprocess.PIPE)
    _, err = await p.communicate()
    return {"ok": p.returncode == 0, "trashed": os.path.basename(path),
            **({} if p.returncode == 0 else {"error": "в корзину убрать не получилось",
                                             "detail": err.decode("utf-8", "replace")[:200]})}


PROGRAM_EXT = {".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".jar", ".appimage", ".desktop", ".deb", ".rpm",
               ".run", ".sh", ".bin", ".py", ".pl", ".flatpakref", ".snap", ".ps1", ".vbs", ".apk", ".lnk"}


def is_program(path: str) -> bool:
    """Исполняемое или установщик: по расширению или по праву на запуск (у обычного документа его нет)."""
    if os.path.splitext(path)[1].lower() in PROGRAM_EXT:
        return True
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except OSError:
        return False


async def call(name, args, session):
    a = args.get("action")
    if a == "find":
        paths = await asyncio.to_thread(search, args.get("query", ""), args.get("kind"))
        if not paths:
            return {"ok": False, "error": f"не нашла файлов по «{args.get('query', '')}»"}
        return {"ok": True, "files": _describe(paths), "note": "назови 2–4 самых вероятных: имя, папка, когда"}
    if a == "recent":
        roots = [_dir("DOWNLOAD", "Downloads"), _dir("DOCUMENTS", "Documents"), _dir("DESKTOP", "Desktop")]
        paths = await asyncio.to_thread(search, "", args.get("kind"), roots, 8)
        if not paths:
            return {"ok": True, "files": [], "note": "в загрузках и документах пусто"}
        return {"ok": True, "files": _describe(paths)}
    path, err = _pick(args.get("n"))
    if err:
        return err
    if a == "open":
        if is_program(path):
            # скачанная программа запустилась бы без вопроса, а её окна Александр не увидит (аудит Fable, B22)
            return {"ok": False, "error": "это программа или установщик — запускать такое я не буду; если она нужна, "
                                          "установим её проверенным способом (system install)"}
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "opened": os.path.basename(path)}
    if a == "read":
        text = await asyncio.to_thread(extract_text, path)
        if not text or not text.strip():
            ext = os.path.splitext(path)[1].lower()
            if ext in KIND_EXT["фото"]:
                return {"ok": False, "error": "это картинка — открой её (open) и попроси описать экран"}
            return {"ok": False, "error": "не смогла достать текст из этого файла"}
        return {**screen._verbatim(text.strip(), f"файл «{os.path.basename(path)}»"), "file": os.path.basename(path)}
    if a == "move":
        dst = folder(args.get("to"))
        if not dst:
            return {"ok": False, "error": "куда: документы, фото, музыка, видео или ксения"}
        name = os.path.basename(path)
        return confirm.ask(f"переложить «{name}» в {os.path.basename(dst)}", lambda: _move(path, dst),
                           question=f"Переложить «{name}» в папку {os.path.basename(dst)}?")
    if a == "trash":
        name = os.path.basename(path)
        return confirm.ask(f"убрать «{name}» в корзину", lambda: _trash(path),
                           question=f"Убрать «{name}» в корзину? Оттуда можно будет вернуть.")
    return {"ok": False, "error": f"неизвестное действие {a}"}
