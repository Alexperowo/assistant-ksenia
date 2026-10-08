"""Сравнение распознавания русской речи на корпусе Dialogs (тест, 16 кГц): WER и скорость."""
import csv, glob, os, re, sys, time
import jiwer, numpy as np, soundfile as sf

D = "/home/user/Agents/Ksenia/data/asr-bench"
rows = {os.path.basename(r[0]): r for r in list(csv.reader(open(f"{D}/test.csv"), delimiter="|"))[1:]}
files = sorted(f for f in glob.glob(f"{D}/wav16/*.wav") if os.path.basename(f) in rows)

def norm(t):
    t = t.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s-]", " ", t).replace("-", " ")
    return " ".join(t.split())

def run(name, fn):
    refs, hyps, t_audio, t0 = [], [], 0.0, time.time()
    for f in files:
        x, sr = sf.read(f, dtype="float32")
        t_audio += len(x) / sr
        hyps.append(norm(fn(x, f)))
        refs.append(norm(rows[os.path.basename(f)][2]))
    dt = time.time() - t0
    wer = jiwer.wer(refs, hyps)
    by_emo = {}
    for r_, h_, f in zip(refs, hyps, files):
        e = rows[os.path.basename(f)][3]
        by_emo.setdefault(e, ([], []))
        by_emo[e][0].append(r_); by_emo[e][1].append(h_)
    worst = sorted(((jiwer.wer(a, b), e) for e, (a, b) in by_emo.items()), reverse=True)[:3]
    print(f"{name:28s} WER {wer*100:5.1f}%  RTF {dt/t_audio:.3f}  ({dt:.0f}s на {t_audio:.0f}s речи)  хуже всего: "
          + ", ".join(f"{e} {w*100:.0f}%" for w, e in worst), flush=True)

which = sys.argv[1:] or ["whisper", "gigaam-v3-e2e-rnnt", "gigaam-v3-e2e-ctc", "t-tech/t-one", "nemo-parakeet-tdt-0.6b-v3", "nemo-canary-1b-v2"]
for w in which:
    try:
        if w == "whisper":
            from faster_whisper import WhisperModel
            m = WhisperModel("/home/user/Models/Speech/whisper/large-v3-turbo", device="cuda", compute_type="int8_float16")
            fn = lambda x, f: " ".join(s.text for s in m.transcribe(x, language="ru", beam_size=5, vad_filter=False,
                                                                  condition_on_previous_text=False)[0])
            fn(np.zeros(16000, dtype="float32"), None)
            run("whisper large-v3-turbo", fn)
        else:
            import onnx_asr
            name, _, path = w.partition("=")
            m = onnx_asr.load_model(name, path or None, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
            w = name
            fn = lambda x, f: m.recognize(f) if False else m.recognize(x, sample_rate=16000, language="ru") if "canary" in w else m.recognize(x, sample_rate=16000)
            fn(np.zeros(16000, dtype="float32"), None)
            run(w, fn)
    except Exception as e:
        print(f"{w}: ОШИБКА {e!r}"[:300], flush=True)
