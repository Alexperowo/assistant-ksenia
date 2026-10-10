import sys, os, base64, re
S = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, S + "/pylib2")
import tiktoken
ranks = {base64.b64decode(t): int(r) for t, r in (l.split() for l in open(S + "/pylib2/dashscope/resources/qwen.tiktoken", "rb"))}
PAT = r"""(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"""
enc = tiktoken.Encoding("qwen", pat_str=PAT, mergeable_ranks=ranks, special_tokens={})
t = open(S + "/snap/docs/USER-GUIDE.md", encoding="utf-8").read()
t = re.sub(r"[#`]", "", t)
w = len(re.findall(r"\w+", t)); n = len(enc.encode(t))
print("words", w, "tokens", n, "tokens/word", round(n/w, 2))
story = ("Жил-был маленький зайчик по имени Пушок. Он жил на опушке большого леса вместе с мамой и тремя сестрёнками. "
 "Каждое утро Пушок выбегал из норки и смотрел, как солнце поднимается над деревьями. Однажды он увидел на тропинке "
 "блестящий камешек и решил, что это волшебная звезда, которая упала с неба. ")
print("story sample tokens/word", round(len(enc.encode(story))/len(re.findall(r'\w+', story)),2))
