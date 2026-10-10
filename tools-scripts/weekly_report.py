#!/usr/bin/env python3
"""Недельная сводка Ксении: мерить, а не гадать (совет из docs/REVIEW-3.md, раздел 10).

Читает logs/core.log, data/live_decisions.jsonl и data/agent_notes.md за последние N дней (по умолчанию 7)
и пишет logs/reports/week-ГГГГ-ММ-ДД.md: сколько было разговоров, как быстро Ксения начинала отвечать,
сколько раз её перебивали и поддакивали, как часто она говорила «Хм, секунду», что решал судья и сколько думал,
какие были ошибки. Ничего не меняет и никуда не отправляет — только на этом компьютере.

Запуск: .venv/bin/python tools-scripts/weekly_report.py [дней]
"""
import ast
import collections
import datetime as dt
import json
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
LOG = os.path.join(ROOT, "logs", "core.log")
DECISIONS = os.path.join(ROOT, "data", "live_decisions.jsonl")
NOTES = os.path.join(ROOT, "data", "agent_notes.md")
OUT = os.path.join(ROOT, "logs", "reports")
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] (.*)$")


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def fmt_s(x):
    return "—" if x is None else f"{x:.1f} с"


def parse_log(since):
    stats = collections.Counter()
    first_audio, llm_done, errors = [], [], collections.Counter()
    days = set()
    if not os.path.exists(LOG):
        return stats, first_audio, llm_done, errors, days
    with open(LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = STAMP.match(line.rstrip("\n"))
            if not m:
                continue
            when = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            if when < since:
                continue
            level, msg = m.group(2), m.group(3)
            if msg.startswith("Александр:"):
                internal = msg.startswith("Александр: (служебно")
                stats["служебные реплики" if internal else "реплики Александра"] += 1
                if not internal:
                    days.add(when.date())
                t = re.search(r"\| (\{.*\})\s*$", msg)
                if t:
                    try:
                        timings = ast.literal_eval(t.group(1))
                    except (ValueError, SyntaxError):
                        timings = {}
                    if not internal and isinstance(timings.get("first_audio_s"), (int, float)):
                        first_audio.append(timings["first_audio_s"])
                    if not internal and isinstance(timings.get("llm_done_s"), (int, float)):
                        llm_done.append(timings["llm_done_s"])
            elif msg.startswith("Живой режим: слушаю постоянно"):
                stats["живых разговоров"] += 1
            elif "перебил" in msg:
                stats["перебивания"] += 1
            elif msg.startswith("Поддакивание"):
                stats["поддакивания (рассказ продолжен)"] += 1
            elif msg.startswith("Долго думает"):
                stats["«Хм, секунду»"] += 1
            elif msg.startswith("Тишина — разговор окончен"):
                stats["разговор закончился тишиной"] += 1
            elif msg.startswith("Кнопка наушников"):
                stats["касания наушников"] += 1
            elif msg.startswith("Инструмент "):
                stats["вызовы инструментов"] += 1
            if level == "ERROR":
                errors[re.sub(r"\d+", "#", msg)[:90]] += 1
    return stats, first_audio, llm_done, errors, days


def parse_decisions(since):
    by = collections.Counter()
    judge_ms, kinds = [], collections.Counter()
    if not os.path.exists(DECISIONS):
        return by, judge_ms, kinds
    with open(DECISIONS, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
                when = dt.datetime.strptime(d["t"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError):
                continue
            if when < since:
                continue
            by[d.get("source", "?")] += 1
            kinds[d.get("kind", "?")] += 1
            if d.get("source") == "judge" and isinstance(d.get("ms"), (int, float)):
                judge_ms.append(d["ms"])
    return by, judge_ms, kinds


def count_notes(since):
    if not os.path.exists(NOTES):
        return 0
    n = 0
    with open(NOTES, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"- (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — ", line)
            if m and dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= since:
                n += 1
    return n


def build(days_back=7, now=None):
    now = now or dt.datetime.now()
    since = now - dt.timedelta(days=days_back)
    stats, first_audio, llm_done, errors, days = parse_log(since)
    by, judge_ms, kinds = parse_decisions(since)
    notes = count_notes(since)
    fast = sum(1 for x in first_audio if x <= 1.5)
    lines = [f"# Сводка Ксении за {days_back} дн. ({since:%d.%m} – {now:%d.%m.%Y})", ""]
    lines += ["## Разговоры", "",
              f"- дней с разговорами: {len(days)}; реплик Александра: {stats['реплики Александра']}; "
              f"живых разговоров: {stats['живых разговоров']}",
              f"- инструментов вызвано: {stats['вызовы инструментов']}; касаний наушников: {stats['касания наушников']}; "
              f"заметок для агента: {notes}", ""]
    lines += ["## Скорость", "",
              f"- до первого звука: медиана {fmt_s(pct(first_audio, 50))}, 90 % ответов быстрее {fmt_s(pct(first_audio, 90))}"
              + (f"; быстрее 1,5 с — {fast * 100 // len(first_audio)} %" if first_audio else ""),
              f"- ответ целиком: медиана {fmt_s(pct(llm_done, 50))}, 90 % — {fmt_s(pct(llm_done, 90))}",
              f"- «Хм, секунду» (молчала дольше 2 с): {stats['«Хм, секунду»']}", ""]
    lines += ["## Живой диалог", "",
              f"- перебивания: {stats['перебивания']}; поддакивания без остановки: "
              f"{stats['поддакивания (рассказ продолжен)']}",
              f"- решения: " + (", ".join(f"{k} {v}" for k, v in by.most_common()) or "нет"),
              f"- что решено: " + (", ".join(f"{k} {v}" for k, v in kinds.most_common()) or "нет"),
              f"- судья думал: медиана {pct(judge_ms, 50) or '—'} мс, 90 % — {pct(judge_ms, 90) or '—'} мс", ""]
    lines += ["## Ошибки", ""]
    lines += [f"- {n} × {msg}" for msg, n in errors.most_common(10)] or ["- нет"]
    lines += ["", "Неверные решения искать в data/live_decisions.jsonl (поля text, kind, acted)."]
    return "\n".join(lines) + "\n"


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    text = build(days_back)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"week-{dt.date.today():%Y-%m-%d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"Сохранено: {path}")
    rotate_logs()


def rotate_logs(limit_mb=10, keep_mb=2):
    """Журналы служб растут без конца (systemd пишет в них с O_APPEND) — раз в неделю, ПОСЛЕ отчёта: больше
    limit_mb — копия в .1 и в самом файле остаются последние keep_mb (аудит Fable, D18)."""
    logs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
    for name in sorted(os.listdir(logs)):
        p = os.path.join(logs, name)
        if not name.endswith(".log") or os.path.getsize(p) < limit_mb * 1024 * 1024:
            continue
        with open(p, "rb") as f:
            data = f.read()
        with open(p + ".1", "wb") as f:
            f.write(data)
        tail = data[-keep_mb * 1024 * 1024:]
        tail = tail[tail.find(b"\n") + 1:]
        with open(p, "r+b") as f:  # тот же файл (служба продолжает дописывать в него), только короче
            f.seek(0)
            f.write(tail)
            f.truncate()
        print(f"Журнал {name}: {len(data) // 1024 // 1024} МБ -> {len(tail) // 1024 // 1024} МБ")


if __name__ == "__main__":
    main()
