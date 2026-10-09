#!/usr/bin/env python3
"""Тихая текстовая автопроверка Ксении: настоящий мозг и инструменты, но в песочнице.

Реплики не попадают в историю разговора и дневник, мозг считает во второй ячейке (кэш разговора цел),
голос уходит в виртуальный выход PipeWire — в комнате тишина. Только безопасные запросы: ничего не
отправляет, не удаляет и не меняет настройки.

    .venv/bin/python tools-scripts/selftest_text.py          # все проверки
    .venv/bin/python tools-scripts/selftest_text.py погода   # только те, где в названии есть «погода»
"""
import json
import subprocess
import sys
import time
import urllib.request

CORE = "http://127.0.0.1:18130"
SINK = "ksenia_selftest"

# (название, реплика, какой инструмент должен сработать — или None, если отвечает сама)
CASES = [
    ("время", "Который час?", None),
    ("погода", "Какая сейчас погода?", "weather"),
    ("прогноз", "Какая погода будет завтра?", "weather"),
    ("музыка", "Что сейчас играет?", "music_status"),
    ("память", "Что ты обо мне помнишь?", "memory_list"),
    ("напоминания", "Какие у меня напоминания?", "remind_list"),
    ("файлы", "Какие файлы я недавно открывал?", "files"),
    ("наушники", "Как там мои наушники?", "headphones"),
    ("самопроверка", "Проверь, всё ли у тебя в порядке.", "self_check"),
    ("новости", "Какие новости про нейросети?", None),
    ("вконтакте", "Мне кто-нибудь писал во ВКонтакте?", "vk_unread"),
    ("справка", "Что ты умеешь?", "help_guide"),
    ("справка-игры", "Во что с тобой можно поиграть?", None),
    ("личность", "Какая у тебя любимая книга?", None),
]

# что не должно звучать в ответе: разметка, служебные слова, английские числа
BAD = ["*", "#", "](", "tool", "json", "ошибка:", "Traceback", "служебн"]


def say(text):
    body = json.dumps({"text": text, "sandbox": True, "sink": SINK}).encode()
    req = urllib.request.Request(CORE + "/say", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    mod = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={SINK}"],
                         capture_output=True, text=True).stdout.strip()
    fails = 0
    try:
        for name, text, tool in CASES:
            if only and only not in name:
                continue
            t0 = time.time()
            try:
                res = say(text)
            except Exception as e:  # ядро не ответило — это провал проверки, идём дальше
                print(f"ПРОВАЛ {name}: ядро не ответило ({e})")
                fails += 1
                continue
            reply = res.get("reply") or ""
            tools = (res.get("timings") or {}).get("tools") or []
            problems = []
            if not reply.strip():
                problems.append("пустой ответ")
            if tool and not any(t["name"] == tool for t in tools):
                problems.append(f"не вызван {tool} (вызваны: {[t['name'] for t in tools] or 'никакие'})")
            if any(not t["ok"] for t in tools if t["name"] == tool):
                problems.append(f"{tool} вернул ошибку")
            problems += [f"в ответе «{b}»" for b in BAD if b.lower() in reply.lower()]
            status = "ПРОВАЛ" if problems else "ок"
            fails += bool(problems)
            print(f"{status:6} {name} ({time.time() - t0:.1f} с): {reply[:160]}")
            for p in problems:
                print(f"       ! {p}")
    finally:
        if mod.isdigit():
            subprocess.run(["pactl", "unload-module", mod])
    print(f"\nИтог: провалов {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
