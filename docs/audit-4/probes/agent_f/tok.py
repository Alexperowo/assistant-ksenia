import sys, os, json, base64
sys.dont_write_bytecode = True
S = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, S + "/pylib2")
import tiktoken
ranks = {}
for line in open(S + "/pylib2/dashscope/resources/qwen.tiktoken", "rb"):
    t, r = line.split()
    ranks[base64.b64decode(t)] = int(r)
PAT = r"""(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"""
enc = tiktoken.Encoding("qwen", pat_str=PAT, mergeable_ranks=ranks, special_tokens={})
sys.path.insert(0, S + "/snap/core"); sys.path.insert(0, S + "/snap/tests")
import logging; logging.disable(logging.CRITICAL)
import conftest, core
def n(s): return len(enc.encode(s))
p = open(S+"/snap/core/prompts/persona.md", encoding="utf-8").read()
s = open(S+"/snap/core/prompts/self.md", encoding="utf-8").read()
tools = json.dumps(core.TOOL_SCHEMAS, ensure_ascii=False)
print("persona chars", len(p), "tokens", n(p))
print("self chars", len(s), "tokens", n(s))
print("tools json chars", len(tools), "tokens", n(tools))
print("PERSONA total", n(core.PERSONA))
# biggest tool schemas
sizes = sorted(((n(json.dumps(t, ensure_ascii=False)), t["function"]["name"]) for t in core.TOOL_SCHEMAS), reverse=True)
print(sizes[:12])
# persona sections
lines = p.splitlines()
print("line35 tool list tokens:", n(lines[34]))
for i,l in enumerate(lines,1):
    pass
print("chars/token persona:", len(p)/n(p))
