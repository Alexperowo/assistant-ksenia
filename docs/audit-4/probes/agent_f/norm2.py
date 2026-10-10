import sys, os, re
sys.dont_write_bytecode = True
S = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, S + "/snap/core")
import speech_norm as sn
src = open(S + "/snap/core/speech_norm.py", encoding="utf-8").read()
# patched copy
src = src.replace('t = re.sub(r"\\b(\\d{1,4})\\s?г\\.(?=[\\s,;)]|$)", r"\\1 года", t)',
                  't = re.sub(r"\\b(\\d{3,4}|\\d{1,4}(?=\\s?г\\.\\s*(?:до\\s+)?(?:нашей|н\\.)))\\s?г\\.(?=[\\s,;)]|$)", r"\\1 года", t)')
src = src.replace('    # единицы после числа', '    t = re.sub(NUM + r"\\s?°\\s?[CС]?(?![\\w])", lambda m: f"{m.group(1)} {plural(m.group(1), \'градус\', \'градуса\', \'градусов\')}", t)\n    # единицы после числа')
src = src.replace('t = re.sub(r"(?:(?<=\\s)|^|(?<=\\())\\+(?=\\d)", "плюс ", t)', 't = re.sub(r"(?:(?<=[\\s(…«])|^)\\+(?=\\d)", "плюс ", t)')
src = src.replace('t = re.sub(r"(?:(?<=\\s)|^|(?<=\\())[−\\-](?=\\d)", "минус ", t)', 't = re.sub(r"(?:(?<=[\\s(…«])|^)[−\\-–](?=\\d)", "минус ", t)')
src = src.replace("по'нял поняла' по'няли при'нял приняла' на'чал начала' на'чали", "по'нял поняла' по'няли при'нял приняла' на'чал на'чали")
ns = {}; exec(compile(src, "speech_norm_patched", "exec"), ns)
for x in ["Ночью до –5.", "Будет «+14».", "Днём +12…+14°.", "Днём 12-14°.", "5 г. сахара", "в 1990 г.", "Это было в 44 г. до н. э.",
          "С самого начала было ясно.", "Она начала петь.", "Счёт 2 -1.", "до −5 и +3°", "10-15 мм", "в 2026 г. и 1990 гг."]:
    print(repr(x), "| old:", repr(sn.normalize(x)), "| new:", repr(ns["normalize"](x)))
