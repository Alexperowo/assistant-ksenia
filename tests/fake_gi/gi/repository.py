import json
import os
import time


class _States:
    ACTIVE, SHOWING, ENABLED, FOCUSED, EDITABLE = "active", "showing", "enabled", "focused", "editable"
    SINGLE_LINE, MULTI_LINE = "single-line", "multi-line"


class _StateSet:
    def __init__(self, states):
        self.states = set(states)

    def contains(self, st):
        return st in self.states


class Node:
    def __init__(self, spec):
        self.spec = spec
        gen = spec.get("generate")
        self._children = None if gen else [Node(c) for c in spec.get("children", [])]
        self.text = spec.get("text", "")
        self.actions_done = []

    def _hang(self):
        if self.spec.get("hang"):
            time.sleep(3600)  # зависшая программа: D-Bus не отвечает

    def get_child_count(self):
        self._hang()
        if self._children is None:
            return self.spec["generate"]
        return len(self._children)

    def get_child_at_index(self, i):
        if self._children is None:
            return Node({"role": "push button", "name": f"Кнопка {i}", "states": ["showing", "enabled"]})
        return self._children[i]

    def get_state_set(self):
        return _StateSet(self.spec.get("states", ["showing", "enabled"]))

    def get_role_name(self):
        return self.spec.get("role", "panel")

    def get_name(self):
        return self.spec.get("name", "")

    def get_n_actions(self):
        return len(self.spec.get("actions", ["press"]))

    def get_action_name(self, i):
        return self.spec.get("actions", ["press"])[i]

    def do_action(self, i):
        log = os.environ.get("FAKE_ATSPI_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"clicked": self.get_name()}, ensure_ascii=False) + "\n")
        return True

    def grab_focus(self):
        return True

    def get_text_iface(self):
        return self if "text" in self.spec else None

    def get_editable_text_iface(self):
        return self if self.spec.get("editable_iface") else None


class Text:
    @staticmethod
    def get_character_count(t):
        return len(t.text)

    @staticmethod
    def get_text(t, a, b):
        return t.text[a:b]

    @staticmethod
    def get_caret_offset(t):
        return 0


class EditableText:
    @staticmethod
    def set_text_contents(et, text):
        et.text = text
        log = os.environ.get("FAKE_ATSPI_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"typed": text}, ensure_ascii=False) + "\n")
        return True

    @staticmethod
    def insert_text(et, pos, text, n):
        log = os.environ.get("FAKE_ATSPI_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"inserted": text}, ensure_ascii=False) + "\n")
        return True


class Atspi:
    StateType = _States
    Text = Text
    EditableText = EditableText

    @staticmethod
    def init():
        pass

    @staticmethod
    def get_desktop(i):
        with open(os.environ["FAKE_ATSPI_TREE"], encoding="utf-8") as f:
            spec = json.load(f)
        return Node({"role": "desktop", "children": spec})
