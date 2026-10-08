#!/usr/bin/python3
"""Помощник дерева доступности (AT-SPI). Запускается СИСТЕМНЫМ python3 (там есть gi/Atspi),
ядро общается с ним через JSON: argv[1] = команда, stdin = параметры, stdout = результат.

Не зависит от экранной лупы и разрешения: элементы находятся по роли и названию, нажимаются
программно (Action), текст вписывается через EditableText — без координат мыши.
"""
import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

ACTIONABLE = {"push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
              "radio menu item", "menu", "link", "page tab", "list item", "combo box", "tree item",
              "table cell", "icon", "button", "spin button", "slider"}
EDITABLE = {"text", "entry", "password text", "editbar", "terminal", "document text", "paragraph"}
MAX_NODES = 3000


def active_window():
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if not app:
            continue
        for j in range(app.get_child_count()):
            w = app.get_child_at_index(j)
            try:
                if w and w.get_state_set().contains(Atspi.StateType.ACTIVE):
                    return app, w
            except Exception:
                continue
    return None, None


def walk(root, limit=MAX_NODES):
    stack, n = [root], 0
    while stack and n < limit:
        node = stack.pop()
        n += 1
        yield node
        try:
            cnt = node.get_child_count()
        except Exception:
            continue
        for k in range(cnt - 1, -1, -1):
            try:
                ch = node.get_child_at_index(k)
            except Exception:
                ch = None
            if ch is not None:
                stack.append(ch)


def info(node):
    try:
        st = node.get_state_set()
        return {"role": node.get_role_name(), "name": (node.get_name() or "").strip(),
                "showing": st.contains(Atspi.StateType.SHOWING),
                "enabled": st.contains(Atspi.StateType.ENABLED),
                "focused": st.contains(Atspi.StateType.FOCUSED),
                "editable": st.contains(Atspi.StateType.EDITABLE)}
    except Exception:
        return None


def norm(s):
    return " ".join((s or "").lower().replace("ё", "е").replace("&", "").split())


def find(win, name, want_editable=False):
    q = norm(name)
    best, best_score = None, 0
    for node in walk(win):
        inf = info(node)
        if not inf or not inf["showing"]:
            continue
        if want_editable and not (inf["editable"] or inf["role"] in EDITABLE):
            continue
        if not want_editable and inf["role"] not in ACTIONABLE:
            continue
        nm = norm(inf["name"])
        if not q:
            score = 1 if (want_editable and inf["focused"]) else 0
        elif nm == q:
            score = 3
        elif nm.startswith(q) or q in nm:
            score = 2
        elif all(w in nm for w in q.split()):
            score = 1
        else:
            score = 0
        if score > best_score:
            best, best_score = node, score
            if score == 3:
                break
    return best


def do_action(node):
    try:
        n = node.get_n_actions()
    except Exception:
        n = 0
    names = []
    for i in range(n):
        names.append((node.get_action_name(i) or "").lower())
    for pref in ("press", "click", "activate", "toggle", "jump", "open", "showmenu", ""):
        for i, an in enumerate(names):
            if an == pref or (pref and pref in an):
                return bool(node.do_action(i))
    if n:
        return bool(node.do_action(0))
    try:
        return bool(node.grab_focus())
    except Exception:
        return False


def text_of(node):
    try:
        t = node.get_text_iface() if hasattr(node, "get_text_iface") else None
        if t is None:
            return ""
        return Atspi.Text.get_text(t, 0, Atspi.Text.get_character_count(t)) or ""
    except Exception:
        return ""


def main():
    cmd = sys.argv[1]
    args = json.loads(sys.stdin.read() or "{}")
    Atspi.init()
    app, win = active_window()
    if not win:
        print(json.dumps({"ok": False, "error": "не вижу активного окна (или у программы нет доступности)"}))
        return
    title, appname = (win.get_name() or "").strip(), (app.get_name() or "").strip()
    if cmd == "list":
        items, seen = [], set()
        for node in walk(win):
            inf = info(node)
            if not inf or not inf["showing"] or not inf["name"]:
                continue
            kind = "field" if (inf["editable"] or inf["role"] in EDITABLE) else (
                "control" if inf["role"] in ACTIONABLE else None)
            if not kind:
                continue
            key = (inf["role"], inf["name"])
            if key in seen:
                continue
            seen.add(key)
            items.append({"kind": kind, "role": inf["role"], "name": inf["name"][:80],
                          **({"disabled": True} if not inf["enabled"] else {}),
                          **({"focused": True} if inf["focused"] else {})})
            if len(items) >= 80:
                break
        print(json.dumps({"ok": True, "app": appname, "window": title, "elements": items}, ensure_ascii=False))
    elif cmd == "click":
        node = find(win, args.get("name", ""))
        if not node:
            print(json.dumps({"ok": False, "error": f"не нашла «{args.get('name')}» в окне «{title}»"}, ensure_ascii=False))
            return
        inf = info(node)
        ok = do_action(node)
        print(json.dumps({"ok": ok, "clicked": inf["name"], "role": inf["role"], "window": title}, ensure_ascii=False))
    elif cmd == "type":
        node = find(win, args.get("field", ""), want_editable=True)
        if not node:
            print(json.dumps({"ok": False, "error": "не нашла поле для ввода"}, ensure_ascii=False))
            return
        try:
            node.grab_focus()
        except Exception:
            pass
        ok = False
        try:
            et = node.get_editable_text_iface()
            if et is not None:
                if args.get("replace", True):
                    ok = Atspi.EditableText.set_text_contents(et, args.get("text", ""))
                else:
                    pos = Atspi.Text.get_caret_offset(node.get_text_iface())
                    ok = Atspi.EditableText.insert_text(et, pos, args.get("text", ""), len(args.get("text", "")))
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"поле не принимает текст: {e}"}, ensure_ascii=False))
            return
        print(json.dumps({"ok": bool(ok), "field": info(node)["name"], "window": title}, ensure_ascii=False))
    elif cmd == "read":
        parts, seen = [], set()
        for node in walk(win):
            inf = info(node)
            if not inf or not inf["showing"]:
                continue
            t = text_of(node).strip() if inf["role"] in EDITABLE or inf["role"] in ("label", "static", "heading") else ""
            t = t or (inf["name"] if inf["role"] in ("label", "heading", "static", "paragraph") else "")
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                parts.append(t)
        print(json.dumps({"ok": True, "app": appname, "window": title, "text": "\n".join(parts)}, ensure_ascii=False))
    elif cmd == "focused":
        for node in walk(win):
            inf = info(node)
            if inf and inf["focused"]:
                print(json.dumps({"ok": True, "window": title, "focused": inf}, ensure_ascii=False))
                return
        print(json.dumps({"ok": True, "window": title, "focused": None}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # помощник никогда не падает молча
        print(json.dumps({"ok": False, "error": f"доступность: {e!r}"[:300]}, ensure_ascii=False))
