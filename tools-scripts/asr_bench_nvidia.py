import sys, os
sys.argv = [sys.argv[0]]
from huggingface_hub import snapshot_download
import importlib.util
spec = importlib.util.spec_from_file_location("b", "/home/user/Agents/Ksenia/tools-scripts/asr_bench.py")
for repo, name in [("istupakov/parakeet-tdt-0.6b-v3-onnx", "nemo-parakeet-tdt-0.6b-v3"), ("istupakov/canary-1b-v2-onnx", "nemo-canary-1b-v2")]:
    d = snapshot_download(repo, local_dir=f"/home/user/Models/Speech/asr/{repo.split('/')[1]}")
    print("downloaded", d, flush=True)
