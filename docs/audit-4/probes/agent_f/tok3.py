import sys, os, base64, json
S = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, S + "/pylib2")
import tiktoken
ranks = {base64.b64decode(t): int(r) for t, r in (l.split() for l in open(S + "/pylib2/dashscope/resources/qwen.tiktoken", "rb"))}
PAT = r"""(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"""
enc = tiktoken.Encoding("qwen", pat_str=PAT, mergeable_ranks=ranks, special_tokens={})
L = open(S + "/snap/core/prompts/persona.md", encoding="utf-8").read().splitlines()
def n(a, b): return len(enc.encode("\n".join(L[a-1:b])))
for name, a, b in [("character 1-8",1,8),("voice/rules 10-29",10,29),("facts 31-33",31,33),("tool list 35",35,35),("tools 36-62",36,62),
                   ("memory..asr 63-88",63,88),("numbers..linux 89-92",89,92),("games 93-102",93,102)]:
    print(f"{name:24s} {n(a,b):5d}")
sys.path.insert(0, S + "/snap/core"); sys.path.insert(0, S + "/snap/tests")
import logging; logging.disable(logging.CRITICAL)
import conftest, core
desc = sum(len(enc.encode(t["function"]["description"])) for t in core.TOOL_SCHEMAS)
print("tool descriptions only", desc, "of", len(enc.encode(json.dumps(core.TOOL_SCHEMAS, ensure_ascii=False))))
print("memory/diary blocks are runtime (data/ not in git) — not counted")
