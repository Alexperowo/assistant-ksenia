"""Мои настройки голосом — те же самые, что в центре управления (control.SETTINGS), один список на оба пути.

Раньше «не жди имени после разговора» или «проверяй сообщения каждую минуту» можно было сказать только кнопкой
в центре: голосовой setting знает лишь настройки компьютера (анализ Codex 2026-10-11, п. 3.1).
"""
import control

CONFIG = None  # ядро подключает свой CONFIG при запуске (core.on_start)

_LIST = "; ".join(f"{s['key']} — {s['label']}" for s in control.SETTINGS)

SCHEMAS = [
    {"type": "function", "function": {
        "name": "ksenia_setting",
        "description": ("МОИ собственные настройки, как в центре управления (не компьютера): " + _LIST + ". "
                        "action get — что сейчас и какие есть варианты; set — поменять, value — да/нет или вариант "
                        "словами («каждую минуту», «тихо», «в 22:00»)."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["get", "set"]},
            "key": {"type": "string", "enum": [s["key"] for s in control.SETTINGS]},
            "value": {"type": "string"}},
            "required": ["action", "key"]}}},
]
TIMEOUTS = {"ksenia_setting": 10}

YES = {"да", "вкл", "включи", "включить", "включено", "on", "true", "1"}
NO = {"нет", "выкл", "выключи", "выключить", "выключено", "off", "false", "0", "не надо"}


def _label(spec, value):
    if spec["type"] == "bool":
        return "включено" if value else "выключено"
    return next((o[1] for o in spec["options"] if o[0] == value), str(value))


def parse(spec, text: str):
    """Слова Александра (через модель) -> значение настройки; непонятное -> ValueError."""
    t = " ".join(str(text or "").lower().replace("ё", "е").split()).strip(" .!")
    if spec["type"] == "bool":
        if t in YES:
            return True
        if t in NO:
            return False
        raise ValueError("нужно да или нет")
    for value, label in spec["options"]:
        if t == label.lower().replace("ё", "е") or t == str(value):
            return value
        try:
            if float(t.replace(",", ".")) == float(value):
                return value
        except ValueError:
            pass
    hits = [value for value, label in spec["options"] if t and t in label.lower().replace("ё", "е")]
    if len(hits) == 1:
        return hits[0]
    raise ValueError("такого варианта нет")


async def call(name, args, session):
    spec = control.KEYS.get(args.get("key"))
    if name != "ksenia_setting" or not spec or CONFIG is None:
        return {"ok": False, "error": "нет такой настройки"}
    options = [o[1] for o in spec.get("options", [])] or ["включено", "выключено"]
    now = CONFIG.get(spec["key"], spec["default"])
    if args.get("action") != "set":
        return {"ok": True, "setting": spec["label"], "now": _label(spec, now), "options": options}
    try:
        v = control.set_setting(CONFIG, spec["key"], parse(spec, args.get("value")))
    except ValueError as e:
        return {"ok": False, "setting": spec["label"], "error": str(e), "options": options}
    return {"ok": True, "setting": spec["label"], "now": _label(spec, v)}
