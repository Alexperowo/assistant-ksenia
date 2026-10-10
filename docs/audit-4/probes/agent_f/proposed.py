import sys, os, re
sys.dont_write_bytecode = True
S = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, S + "/snap/core"); sys.path.insert(0, S + "/snap/tests"); sys.path.append(S + "/pylib3")
import logging; logging.disable(logging.CRITICAL)
import conftest, core

# ---- tag limiter simulation (exact copy of core lines 1198-1199 + tag_too_often)
class K: pass
k = K(); k.last_tag = None; k.recent_tags = []
voiced = []
for raw in ["[teasing] a", "[teasing] b", "[laughing] c", "[warm] d", "e", "[sigh] f", "g", "h", "[excited] i"]:
    mt = re.match(r"\s*\[(\w+)\]\s*", raw)
    stripped = bool(mt and core.Ksenia.tag_too_often(k, mt.group(1)))
    voiced.append((raw, "STRIPPED" if stripped else "voiced"))
    k.last_tag = (re.match(r"\s*\[(\w+)\]", raw) or [None, None])[1]
    k.recent_tags = (k.recent_tags + [k.last_tag])[-3:]
print("tag limiter:", voiced)

# ---- proposed fix_english_numbers: skip when a neighbouring word is Latin (titles, names)
LAT = re.compile(r"[A-Za-z]")
def fix_en(text):
    def rep(m):
        left = re.search(r"([A-Za-zА-Яа-яЁё]+)[^A-Za-zА-Яа-яЁё]*$", text[:m.start()])
        right = re.match(r"[^A-Za-zА-Яа-яЁё]*([A-Za-zА-Яа-яЁё]+)", text[m.end():])
        if (left and LAT.match(left.group(1))) or (right and LAT.match(right.group(1))):
            return m.group(0)
        return core.EN_NUM_RE.sub(lambda mm: core.fix_english_numbers(mm.group(0)), m.group(0))
    return core.EN_NUM_RE.sub(rep, text)
print("--- fix_en proposed")
for x in ["Включаю Twenty One Pilots — Stressed Out.", "Играет Nine Inch Nails.", "Metallica — One, отличная вещь.",
          "Seven Nation Army", "Dave Brubeck — Take Five", "плюс thirteen", "plus seventeen градусов", "Сейчас twenty-one градус",
          "Thirteen градусов и ветер", "Pearl Jam, альбом Ten", "It's one of the best", "Окей, one moment", "минус five, ясно"]:
    print("  ", repr(x), "->", repr(fix_en(x)))

# ---- proposed feminine tweaks
NOUN_STOP = {"футбол","волейбол","баскетбол","гол","сериал","канал","сигнал","материал","финал","журнал","идеал","зал","бал",
             "вокал","портал","оригинал","интервал","потенциал","стол","пол","фестиваль","генерал","арсенал","мундиал","анекдотал"}
def _fem2(word, nxt):
    low = word.lower()
    if low == "один" and re.match(r"\s+раз", nxt): return word
    if low in NOUN_STOP: return word
    m = re.fullmatch(r"(\w+?[аяеиыуо])лс[яь]", low)
    if m and len(low) > 5: out = m.group(1) + "лась"
    elif low == "ошибся": out = "ошиблась"
    else:
        # generic -л rule only if the next word is not a feminine past verb ("Я сериал досмотрела")
        if re.search(r"[аяеиыуо]л$", low) and low not in core.FEM_ADJ and re.match(r"\s+[а-яё]+(?:ла|лась)\b", nxt):
            return word
        return core._feminine(word)
    return out[0].upper() + out[1:] if word[0].isupper() else out
QUOTE = re.compile(r"«[^»]*»|\"[^\"]*\"|„[^“]*“")
def feminine2(text):
    parts, last = [], 0
    for q in QUOTE.finditer(text):      # чужая речь в кавычках — как есть
        parts.append(("x", text[last:q.start()])); parts.append(("q", q.group(0))); last = q.end()
    parts.append(("x", text[last:]))
    out = []
    for kind, s in parts:
        if kind == "q": out.append(s); continue
        out.append(core.FEM_RE.sub(lambda m: m.group(1) + _fem2(m.group(2), m.string[m.end():]), s))
    return "".join(out)
print("--- feminine proposed")
for x in ["Я понял.", "Я сам могу включить.", "Ты говоришь «я устал» — давай отдохнём.", "Я один раз там была.", "Я один из них.",
          "Я тоже футбол люблю.", "Я сериал досмотрела.", "Я канал переключила.", "Я сигнал услышала.", "Я тебе материал нашла.",
          "Я заблудился в меню.", "Я обрадовался.", "Я ошибся.", "Я понял тебя.", "Я сказал маме.", "Я видел твою фотографию.",
          "Я начал читать.", "Я бы тоже хотел.", "Я идеал.", "Я его нашёл, представляешь"]:
    print("  ", repr(x), "->", repr(feminine2(x)))

# ---- "обо" fix
OBO = re.compile(r"\b([Оо])бо(\s+)(?!(?:мне|всём|всем|всех|всё|все|что|чём|льду)\b)(?=([А-Яа-яЁё]))")
def obo(text):
    return OBO.sub(lambda m: m.group(1) + ("б" if m.group(3).lower() in "аоуэиы" else "") + m.group(2), text)
print("--- obo")
for x in ["Расскажи обо тебе.", "Я думала обо этом.", "Обо мне не волнуйся.", "Обо всём по порядку.", "Споткнулась обо что-то.",
          "Поговорим обо Маше.", "обо игре", "обо ёлке", "Обо всех"]:
    print("  ", repr(x), "->", repr(obo(x)))

# ---- pymorphy3: case after "для"/"у"/"от"/"без"/"из"/"до"/"около"/"кроме" (genitive-only prepositions)
import pymorphy3
morph = pymorphy3.MorphAnalyzer()
GEN_PREP = r"для|у|от|без|из|до|около|кроме|возле|после|вместо|среди|ради"
def fix_case(text):
    def rep(m):
        w = m.group(2)
        ps = morph.parse(w)
        if not ps or any("gent" in p.tag for p in ps) or not all(("NOUN" in p.tag and "Name" in p.tag) for p in ps):
            return m.group(0)  # only proper first names, only if no genitive reading at all
        g = ps[0].inflect({"gent"})
        if not g: return m.group(0)
        nw = g.word.capitalize() if w[0].isupper() else g.word
        return m.group(1) + nw
    return re.sub(r"(\b(?:" + GEN_PREP + r")\s+)([А-ЯЁ][а-яё]+)", rep, text)
print("--- case after genitive prepositions (names only)")
for x in ["Сказка для Машу и Насти.", "Подарок для Маши.", "Письмо от Сашу.", "У Насте день рождения.", "для Маша", "Для Москву",
          "до Петра", "из Ивану", "кроме Аню", "Для Ани и Машу"]:
    print("  ", repr(x), "->", repr(fix_case(x)))
print("--- regression vs existing tests")
print(fix_en("сейчас плюс thirteen, от plus four до plus eleven"))
cases = [("Я понял тебя.", "Я поняла тебя."), ("я рад, что ты спросил", "я рада, что ты спросил"),
    ("Я уже сказал.", "Я уже сказала."), ("Я не уверен.", "Я не уверена."), ("Я пошёл бы", "Я пошла бы"),
    ("Я вышел", "Я вышла"), ("Я тебе говорил", "Я тебе говорила"), ("Я сам не знаю", "Я сама не знаю"),
    ("Я согласен.", "Я согласна."), ("Я не мог", "Я не могла"), ("Я был там", "Я была там"), ("Я прочёл", "Я прочла")]
print(all(feminine2(a) == b for a, b in cases), [ (a, feminine2(a)) for a,b in cases if feminine2(a)!=b])
for t in ["Он сказал, что я права.", "Я поняла.", "Яблоко упал на стол", "Ты сказал, а я слушаю.", "Мой брат сказал"]:
    assert feminine2(t) == t, t
print("leaves-others ok")
