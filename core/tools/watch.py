"""Правила «от кого говорить сразу»: «Ксения, от Курьера говори мне сразу и напоминай».

По умолчанию сообщения — только по вопросу («что пришло?»), но от выбранных людей и чатов — сразу
(решение Александра 2026-10-09). Правило задаётся голосом и хранится и в правилах, и в памяти Ксении.
Слежение: раз в watch_poll_s ядро смотрит список диалогов ВКонтакте в отдельной вкладке браузера (переписки
не открываются — прочитанными не становятся). Новое непрочитанное от выбранного — Ксения говорит сразу;
если так и висит непрочитанным — напоминает через watch_remind_s, не больше watch_remind_max раз.
Telegram — те же правила, когда будет вход.
"""
import json
import os
import time

from tools import confirm, browser_core, memory, vk

ROOT = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.normpath(os.path.join(ROOT, "..", "..", "data", "watch_rules.json"))
STATE = os.path.normpath(os.path.join(ROOT, "..", "..", "data", "watch_state.json"))
ANNOUNCE = {"fn": None}  # ядро ставит сюда функцию «сказать служебно» (без кругового импорта)

SCHEMAS = [
    {"type": "function", "function": {
        "name": "watch_rule",
        "description": ("Правила уведомлений: от кого сообщать о новых сообщениях СРАЗУ (остальное — только по вопросу). "
                        "add — добавить (who — имя или название чата, как во ВКонтакте; remind=true — напоминать, пока "
                        "не прочитано), remove — убрать, list — какие есть."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["add", "remove", "list"]},
            "who": {"type": "string"}, "source": {"type": "string", "enum": ["vk", "telegram"]},
            "remind": {"type": "boolean"}}, "required": ["action"]}}},
]
TIMEOUTS = {"watch_rule": 20}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def rules():
    r = _load(RULES, [])
    return [x for x in r if isinstance(x, dict) and x.get("who")] if isinstance(r, list) else []


async def call(name, args, session):
    a = args.get("action")
    who = " ".join((args.get("who") or "").split())
    src = args.get("source") or "vk"
    rs = rules()
    if a == "list":
        return {"ok": True, "rules": [{"who": r["who"], "source": r["source"], "remind": r.get("remind", True)} for r in rs],
                **({} if rs else {"note": "правил нет — о сообщениях говорю только по вопросу"})}
    if not who:
        return {"ok": False, "error": "от кого? назови имя или чат"}
    if a == "add":
        # имя попадает в память и подсказку мозга: длинный «who» из чужого текста был бы внедрением команд
        # (аудит Fable, B5) — только короткое имя и только после «да» Александра (с планшета — его нажатие)
        who = who.strip("«»\"'“” ").strip()
        if not who or len(who) > 60 or any(ch in who for ch in "\n:;{}<>"):
            return {"ok": False, "error": "назови коротко, как человек или чат подписан во ВКонтакте"}
        remind = bool(args.get("remind", True))

        async def add():
            left = [r for r in rules() if not (r["who"].lower() == who.lower() and r["source"] == src)]
            left.append({"who": who, "source": src, "remind": remind, "added": time.strftime("%Y-%m-%d")})
            _save(RULES, left)
            await memory._remember(f"о сообщениях от «{who}» ({'ВКонтакте' if src == 'vk' else 'Telegram'}) "
                                   f"говорить сразу" + (" и напоминать, пока не прочитано" if remind else ""))
            note = "Telegram пока не подключён — правило заработает после входа" if src == "telegram" else ""
            return {"ok": True, "added": who, "source": src, **({"note": note} if note else {})}

        if args.get("_from_control"):
            return await add()
        return confirm.ask(f"сообщать сразу о сообщениях от «{who}»", add,
                           question=f"Говорить сразу, когда напишет «{who}»?")
    if a == "remove":
        left = [r for r in rs if r["who"].lower() != who.lower()]
        if len(left) == len(rs):
            return {"ok": False, "error": f"правила для «{who}» нет"}
        _save(RULES, left)
        facts = memory._load()
        keep = [f for f in facts if not f["fact"].lower().startswith(f"о сообщениях от «{who.lower()}»")]
        if len(keep) != len(facts):
            memory._save(keep)
            memory.changed["flag"] = True
        return {"ok": True, "removed": who}
    return {"ok": False, "error": f"неизвестное действие {a}"}


def _due(state, key, sig, remind, now, remind_s, remind_max):
    """Что сказать про чат: "new" (новое), "remind" (висит непрочитанным), None. Меняет state."""
    st = state.get(key)
    if st is None or st.get("sig") != sig:
        state[key] = {"sig": sig, "t": now, "n": 0}
        return "new"
    if remind and now - st["t"] >= remind_s and st["n"] < remind_max:
        st["t"], st["n"] = now, st["n"] + 1
        return "remind"
    return None


async def poll(cfg):
    """Один обход: новые непрочитанные от выбранных во ВКонтакте -> ANNOUNCE."""
    vk_rules = [r for r in rules() if r["source"] == "vk"]
    if not vk_rules or not ANNOUNCE["fn"]:
        return 0
    pg = await browser_core.page("vk_watch")  # своя вкладка: не мешает командам «прочитай переписку»
    await pg.goto(vk.IM_URL, wait_until="domcontentloaded", timeout=30000)
    await pg.wait_for_selector(vk.ITEM, timeout=20000)
    await pg.wait_for_timeout(800)
    items = await vk._list_items(pg)
    state, now, said = _load(STATE, {}), time.time(), 0
    unread_names = set()
    for it in items:
        if not it["unread"]:
            continue
        rule = next((r for r in vk_rules if vk._match(r["who"], it["name"])), None)
        if not rule:
            continue
        unread_names.add(it["name"])
        what = _due(state, it["name"], f"{it['unread']}|{it['preview'][:80]}", rule.get("remind", True), now,
                    cfg.get("watch_remind_s", 600), cfg.get("watch_remind_max", 3))
        if what:
            said += 1
            await ANNOUNCE["fn"](what, it["name"], it["preview"][:200], it["unread"])
    for k in list(state):  # прочитал — забыть
        if k not in unread_names:
            del state[k]
    _save(STATE, state)
    return said
