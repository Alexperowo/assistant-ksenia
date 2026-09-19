from __future__ import annotations

import re


_UNITS = (
    "",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
)
_FEMININE_UNITS = ("", "одна", "две") + _UNITS[3:]
_TEENS = (
    "десять",
    "одиннадцать",
    "двенадцать",
    "тринадцать",
    "четырнадцать",
    "пятнадцать",
    "шестнадцать",
    "семнадцать",
    "восемнадцать",
    "девятнадцать",
)
_TENS = (
    "",
    "",
    "двадцать",
    "тридцать",
    "сорок",
    "пятьдесят",
    "шестьдесят",
    "семьдесят",
    "восемьдесят",
    "девяносто",
)
_HUNDREDS = (
    "",
    "сто",
    "двести",
    "триста",
    "четыреста",
    "пятьсот",
    "шестьсот",
    "семьсот",
    "восемьсот",
    "девятьсот",
)
_SCALES = (
    ("", "", "", False),
    ("тысяча", "тысячи", "тысяч", True),
    ("миллион", "миллиона", "миллионов", False),
    ("миллиард", "миллиарда", "миллиардов", False),
    ("триллион", "триллиона", "триллионов", False),
)
_DIGITS = (
    "ноль",
    "один",
    "два",
    "три",
    "четыре",
    "пять",
    "шесть",
    "семь",
    "восемь",
    "девять",
)
_DAY_ORDINALS = (
    "",
    "первое",
    "второе",
    "третье",
    "четвёртое",
    "пятое",
    "шестое",
    "седьмое",
    "восьмое",
    "девятое",
    "десятое",
    "одиннадцатое",
    "двенадцатое",
    "тринадцатое",
    "четырнадцатое",
    "пятнадцатое",
    "шестнадцатое",
    "семнадцатое",
    "восемнадцатое",
    "девятнадцатое",
    "двадцатое",
    "двадцать первое",
    "двадцать второе",
    "двадцать третье",
    "двадцать четвёртое",
    "двадцать пятое",
    "двадцать шестое",
    "двадцать седьмое",
    "двадцать восьмое",
    "двадцать девятое",
    "тридцатое",
    "тридцать первое",
)
_MONTHS = {
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
}
_MONTH_BY_NUMBER = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _plural_index(value: int) -> int:
    value = abs(value) % 100
    if 11 <= value <= 19:
        return 2
    tail = value % 10
    if tail == 1:
        return 0
    if 2 <= tail <= 4:
        return 1
    return 2


def _triad_words(value: int, *, feminine: bool = False) -> list[str]:
    words: list[str] = []
    hundreds, remainder = divmod(value, 100)
    if hundreds:
        words.append(_HUNDREDS[hundreds])
    if 10 <= remainder <= 19:
        words.append(_TEENS[remainder - 10])
        return words
    tens, units = divmod(remainder, 10)
    if tens:
        words.append(_TENS[tens])
    if units:
        words.append((_FEMININE_UNITS if feminine else _UNITS)[units])
    return words


def integer_to_russian_words(value: int) -> str:
    """Spell a reasonably sized integer without adding grammatical context."""
    if value == 0:
        return _DIGITS[0]
    sign = "минус " if value < 0 else ""
    value = abs(value)
    if value >= 10 ** (3 * len(_SCALES)):
        return sign + " ".join(_DIGITS[int(digit)] for digit in str(value))
    triads: list[int] = []
    while value:
        value, triad = divmod(value, 1000)
        triads.append(triad)
    words: list[str] = []
    for scale_index in range(len(triads) - 1, -1, -1):
        triad = triads[scale_index]
        if not triad:
            continue
        forms = _SCALES[scale_index]
        words.extend(_triad_words(triad, feminine=forms[3]))
        if scale_index:
            words.append(forms[_plural_index(triad)])
    return sign + " ".join(words)


_ORDINAL_LAST_WORD = {
    "один": "первый",
    "два": "второй",
    "три": "третий",
    "четыре": "четвёртый",
    "пять": "пятый",
    "шесть": "шестой",
    "семь": "седьмой",
    "восемь": "восьмой",
    "девять": "девятый",
    "десять": "десятый",
    "одиннадцать": "одиннадцатый",
    "двенадцать": "двенадцатый",
    "тринадцать": "тринадцатый",
    "четырнадцать": "четырнадцатый",
    "пятнадцать": "пятнадцатый",
    "шестнадцать": "шестнадцатый",
    "семнадцать": "семнадцатый",
    "восемнадцать": "восемнадцатый",
    "девятнадцать": "девятнадцатый",
    "двадцать": "двадцатый",
    "тридцать": "тридцатый",
    "сорок": "сороковой",
    "пятьдесят": "пятидесятый",
    "шестьдесят": "шестидесятый",
    "семьдесят": "семидесятый",
    "восемьдесят": "восьмидесятый",
    "девяносто": "девяностый",
    "сто": "сотый",
}


def _year_genitive(value: int) -> str:
    if value == 2000:
        return "двухтысячного"
    words = integer_to_russian_words(value).split()
    if not words:
        return str(value)
    ordinal = _ORDINAL_LAST_WORD.get(words[-1])
    if ordinal is None:
        return " ".join(words)
    if ordinal.endswith("ий"):
        ordinal = ordinal[:-2] + "его"
    elif ordinal.endswith(("ый", "ой")):
        ordinal = ordinal[:-2] + "ого"
    words[-1] = ordinal
    return " ".join(words)


def _replace_numeric_date(match: re.Match[str]) -> str:
    day = int(match.group("day"))
    month = int(match.group("month"))
    year = int(match.group("year"))
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return match.group(0)
    return f"{_DAY_ORDINALS[day]} {_MONTH_BY_NUMBER[month]} {_year_genitive(year)} года"


def _replace_written_date(match: re.Match[str]) -> str:
    day = int(match.group("day"))
    month = match.group("month")
    year = int(match.group("year"))
    if not 1 <= day <= 31 or month.casefold() not in _MONTHS:
        return match.group(0)
    return f"{_DAY_ORDINALS[day]} {month} {_year_genitive(year)} года"


def _replace_time(match: re.Match[str]) -> str:
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return match.group(0)
    hour_forms = ("час", "часа", "часов")
    minute_forms = ("минута", "минуты", "минут")
    return (
        f"{integer_to_russian_words(hours)} {hour_forms[_plural_index(hours)]} "
        f"{integer_to_russian_words(minutes)} {minute_forms[_plural_index(minutes)]}"
    )


_SPECIAL_TERMS = {
    "llama": "Лама",
    "deepseek": "Дипсик",
    "mistral": "Мистраль",
    "whisper": "Виспер",
    "silero": "Силеро",
    "vosk": "Воск",
    "pytorch": "Пайторч",
    "python": "Пайтон",
    "windows": "Виндовс",
    "word": "Ворд",
    "excel": "Эксель",
    "powerpoint": "Пауэрпоинт",
    "powershell": "Пауэршелл",
    "cmd": "ЦМД",
    "explorer": "Проводник",
    "chrome": "Хром",
    "chromium": "Хромиум",
    "edge": "Эдж",
    "firefox": "Файрфокс",
    "vscode": "Ви-Эс Код",
    "code": "Код",
    "github": "Гитхаб",
    "git": "Гит",
    "google": "Гугл",
    "alibaba": "Алибаба",
    "cloud": "Клауд",
    "openai": "Опен-Эй-Ай",
    "microsoft": "Майкрософт",
    "apple": "Эппл",
    "meta": "Мета",
    "quest": "Квест",
    "oculus": "Окулус",
    "jbl": "Джи-Би-Эль",
    "tour": "Тур",
    "sense": "Сенс",
    "one": "Ван",
    "pro": "Про",
    "plus": "Плюс",
    "max": "Макс",
    "ultra": "Ультра",
    "lite": "Лайт",
    "mini": "Мини",
    "realtek": "Риалтек",
    "nvidia": "Нвидиа",
    "geforce": "Джифорс",
    "rtx": "Эр-Тэ-Икс",
    "gtx": "Джи-Тэ-Икс",
    "cuda": "Куда",
    "cpu": "ЦПУ",
    "gpu": "ГПУ",
    "vram": "видеопамять",
    "ram": "ОЗУ",
    "ssd": "ССД",
    "hdd": "ХДД",
    "usb": "Ю-Эс-Би",
    "wi-fi": "вай-фай",
    "wifi": "вай-фай",
    "bluetooth": "блютуз",
    "gguf": "Гэ-Гэ-У-Эф",
    "llm": "ЛЛМ",
    "api": "А-Пи-Ай",
    "url": "Ю-Эр-Эль",
    "http": "Эйч-Ти-Ти-Пи",
    "https": "Эйч-Ти-Ти-Пи-Эс",
    "ip": "Ай-Пи",
    "pin": "пин",
    "id": "Ай-Ди",
    "os": "О-Эс",
    "pc": "Пи-Си",
    "gb": "гигабайт",
    "mb": "мегабайт",
    "kb": "килобайт",
    "tb": "терабайт",
    "ghz": "гигагерц",
    "mhz": "мегагерц",
    "hz": "герц",
    "fps": "кадров в секунду",
    "ms": "миллисекунд",
    "sec": "секунд",
    "min": "минут",
    "docx": "документ ворд",
    "doc": "документ ворд",
    "txt": "текстовый файл",
    "pdf": "пэдээф",
    "json": "джейсон",
    "html": "эйч-ти-эм-эль",
    "xml": "икс-эм-эль",
    "zip": "зип",
    "exe": "экзе",
}

_DIGRAPHS = (
    ("tion", "шн"),
    ("sion", "жн"),
    ("ight", "айт"),
    ("sch", "ш"),
    ("sh", "ш"),
    ("ch", "ч"),
    ("th", "т"),
    ("ph", "ф"),
    ("qu", "кв"),
    ("ck", "к"),
    ("wh", "в"),
    ("kn", "н"),
    ("wr", "р"),
    ("ng", "нг"),
    ("ee", "и"),
    ("oo", "у"),
    ("ea", "и"),
    ("ai", "эй"),
    ("ay", "эй"),
    ("ei", "эй"),
    ("ey", "эй"),
    ("oi", "ой"),
    ("oy", "ой"),
    ("ou", "ау"),
    ("ow", "ау"),
    ("au", "о"),
    ("aw", "о"),
    ("ia", "иа"),
    ("io", "ио"),
    ("iu", "иу"),
)

_SINGLE_LETTERS = {
    "b": "б",
    "c": "к",
    "d": "д",
    "f": "ф",
    "g": "г",
    "h": "х",
    "j": "дж",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "v": "в",
    "w": "в",
    "x": "кс",
    "z": "з",
    "a": "а",
    "e": "е",
    "i": "и",
    "o": "о",
    "u": "у",
    "y": "и",
}


def transliterate_latin_to_cyrillic(word: str) -> str:
    """Phonetically transcribe a Latin word into Russian Cyrillic for Silero TTS."""
    cf = word.casefold()
    if cf in _SPECIAL_TERMS:
        res = _SPECIAL_TERMS[cf]
        if word.isupper() and len(word) > 1:
            return res.upper() if "-" not in res else res
        return res if (word.islower() or res[0].isupper()) else res.capitalize()
    letters: list[str] = []
    i = 0
    w = cf
    length = len(w)
    while i < length:
        matched = False
        for pattern, repl in _DIGRAPHS:
            if w.startswith(pattern, i):
                letters.append(repl)
                i += len(pattern)
                matched = True
                break
        if matched:
            continue
        c = w[i]
        if c in {"c", "g"} and i + 1 < length and w[i + 1] in {"e", "i", "y"}:
            letters.append("с" if c == "c" else "дж")
        elif c in _SINGLE_LETTERS:
            letters.append(_SINGLE_LETTERS[c])
        else:
            letters.append(c)
        i += 1
    result = "".join(letters)
    return result.capitalize() if word.istitle() else result


def _replace_decimal(match: re.Match[str]) -> str:
    whole = int(match.group("whole"))
    fraction = match.group("fraction")
    fraction_words = (
        " ".join(_DIGITS[int(digit)] for digit in fraction)
        if fraction.startswith("0") or len(fraction) > 3
        else integer_to_russian_words(int(fraction))
    )
    separator = "запятая" if match.group("separator") == "," else "точка"
    return f"{integer_to_russian_words(whole)} {separator} {fraction_words}"


_RUSSIAN_STRESS_MAP: dict[str, str] = {
    "готов": "гот+ов",
    "готова": "гот+ова",
    "готово": "гот+ово",
    "готовы": "гот+овы",
    "подготовлен": "подгот+овлен",
    "подготовлена": "подгот+овлена",
    "подготовлено": "подгот+овлено",
    "подготовлены": "подгот+овлены",
    "подготовка": "подгот+овка",
    "готовлю": "гот+овлю",
    "готовит": "гот+овит",
    "готовьте": "гот+овьте",
    "включен": "включ+ен",
    "включена": "включен+а",
    "включено": "включен+о",
    "включены": "включен+ы",
    "отключен": "отключ+ен",
    "отключена": "отключен+а",
    "отключено": "отключен+о",
    "отключены": "отключен+ы",
    "звонит": "звон+ит",
    "звонят": "звон+ят",
    "понял": "п+онял",
    "поняла": "понял+а",
    "понятно": "пон+ятно",
    "создан": "с+оздан",
    "создана": "создан+а",
    "создано": "с+оздано",
    "начал": "н+ачал",
    "начала": "начал+а",
    "начало": "нач+ало",
    "занят": "з+анят",
    "занята": "занят+а",
    "занято": "з+анято",
}

_STRESS_REGEX = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _RUSSIAN_STRESS_MAP) + r")\b",
    flags=re.IGNORECASE,
)


def _apply_russian_stress(match: re.Match[str]) -> str:
    word = match.group(0)
    key = word.casefold()
    replacement = _RUSSIAN_STRESS_MAP.get(key, word)
    if word.isupper() and len(word) > 1:
        return replacement.upper()
    if word.istitle():
        return replacement.capitalize()
    return replacement


def normalize_for_speech(text: str) -> str:
    """Make digits and Latin audible for Silero while keeping text shown unchanged."""
    value = str(text)
    # Remove ellipses to prevent Silero saying 'три точки'
    value = re.sub(r"\.{2,}|…", ", ", value)
    # Remove markdown asterisks, hashes, backticks, brackets
    value = re.sub(r"[*#`_~]", "", value)
    # Separate attached letters and digits: Model3.5 -> Model 3.5
    value = re.sub(r"([A-Za-zА-Яа-яЁё])(\d)", r"\1 \2", value)
    value = re.sub(r"(\d)([A-Za-zА-Яа-яЁё])", r"\1 \2", value)
    value = re.sub(
        r"(?<!\d)(?P<day>0?[1-9]|[12]\d|3[01])[./-](?P<month>0?[1-9]|1[0-2])[./-](?P<year>\d{4})(?!\d)",
        _replace_numeric_date,
        value,
    )
    months = "|".join(sorted(_MONTHS, key=len, reverse=True))
    value = re.sub(
        rf"(?<!\d)(?P<day>0?[1-9]|[12]\d|3[01])\s+(?P<month>{months})\s+(?P<year>\d{{4}})\s+года\b",
        _replace_written_date,
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"(?<!\d)(?P<hours>[01]?\d|2[0-3]):(?P<minutes>[0-5]\d)(?!\d)",
        _replace_time,
        value,
    )
    value = re.sub(r"№\s*(\d+)", lambda match: f"номер {integer_to_russian_words(int(match.group(1)))}", value)
    value = re.sub(
        r"(?<!\d)(?P<whole>-?\d+)(?P<separator>[.,])(?P<fraction>\d+)(?!\d)",
        _replace_decimal,
        value,
    )
    value = re.sub(
        r"(?<!\d)([+-]?\d+)(?!\d)",
        lambda match: (
            "плюс " + integer_to_russian_words(int(match.group(1)[1:]))
            if match.group(1).startswith("+")
            else integer_to_russian_words(int(match.group(1)))
        ),
        value,
    )
    # Transliterate Latin words into Russian Cyrillic phonemes
    value = re.sub(r"[A-Za-z]+", lambda match: transliterate_latin_to_cyrillic(match.group(0)), value)
    # Apply phonetic stress marks for Russian homographs and words misstressed by Silero
    value = _STRESS_REGEX.sub(_apply_russian_stress, value)
    return re.sub(r"[ \t]+", " ", value).strip()

