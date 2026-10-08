"""Узнавание говорящего: EER на корпусе Dialogs (3 актёра), чисто и со сжатием «как Bluetooth» (opus 16 кГц, 24 кбит/с)."""
import csv, glob, os, subprocess, time
import numpy as np, onnxruntime as ort, soundfile as sf, torch, torchaudio

D = "/home/user/Agents/Ksenia/data/asr-bench"; M = "/home/user/Models/Speech/speaker"
rows = {os.path.basename(r[0]): r for r in list(csv.reader(open(f"{D}/test.csv"), delimiter="|"))[1:]}
files = sorted(f for f in glob.glob(f"{D}/wav16/*.wav") if os.path.basename(f) in rows)
os.makedirs(f"{D}/bt16", exist_ok=True)
for f in files:  # искажение «как в гарнитуре»: сжатие opus 24 кбит/с и обратно
    out = f"{D}/bt16/" + os.path.basename(f)
    if not os.path.exists(out):
        subprocess.run(f"ffmpeg -loglevel error -y -i {f} -c:a libopus -b:a 24k -ar 16000 -f ogg - | ffmpeg -loglevel error -y -i - -ar 16000 -ac 1 {out}", shell=True)

def kaldi_fbank(x):  # WeSpeaker / ECAPA-подобные
    w = torch.from_numpy(x).unsqueeze(0) * 32768
    f = torchaudio.compliance.kaldi.fbank(w, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0, sample_frequency=16000)
    return (f - f.mean(0, keepdim=True)).numpy()

def nemo_mel(x):  # TitaNet: NeMo preprocessor (n_fft 512, win 400, hop 160, 80 mel, log, per-feature norm)
    w = torch.from_numpy(x).unsqueeze(0)
    m = torchaudio.transforms.MelSpectrogram(16000, n_fft=512, win_length=400, hop_length=160, n_mels=80, power=2.0,
                                             window_fn=torch.hann_window, norm="slaney", mel_scale="slaney")(w)
    m = torch.log(m + 2 ** -24)[0]
    m = (m - m.mean(1, keepdim=True)) / (m.std(1, keepdim=True) + 1e-5)
    return m.numpy()

sess = {n: ort.InferenceSession(f"{M}/{f}", providers=["CPUExecutionProvider"]) for n, f in
        [("titanet", "titanet-large.onnx"), ("ecapa", "ecapa_tdnn.onnx"), ("wespeaker", "wespeaker-resnet34.onnx")]}

def embed(name, x):
    if name == "titanet":
        m = nemo_mel(x)[None]
        e = sess[name].run(["embs"], {"audio_signal": m.astype(np.float32), "length": np.array([m.shape[2]], np.int64)})[0][0]
    elif name == "ecapa":
        e = sess[name].run(None, {"feats": kaldi_fbank(x)[None].astype(np.float32)})[0].reshape(-1)
    else:
        e = sess[name].run(None, {"input_features": kaldi_fbank(x)[None].astype(np.float32)})[0][0]
    return e / np.linalg.norm(e)

def eer(tgt, non):
    s = np.r_[tgt, non]; y = np.r_[np.ones(len(tgt)), np.zeros(len(non))]
    best = 1
    for t in np.unique(s):
        far = ((s >= t) & (y == 0)).sum() / len(non); frr = ((s < t) & (y == 1)).sum() / len(tgt)
        best = min(best, max(far, frr))
    return best

for cond in ("wav16", "bt16"):
    for name in sess:
        t0 = time.time()
        E = {f: embed(name, sf.read(f"{D}/{cond}/" + os.path.basename(f), dtype="float32")[0]) for f in files}
        spk = {f: rows[os.path.basename(f)][1] for f in files}
        enroll = {s: [f for f in files if spk[f] == s][:3] for s in set(spk.values())}
        cent = {s: np.mean([E[f] for f in fs], 0) for s, fs in enroll.items()}
        cent = {s: c / np.linalg.norm(c) for s, c in cent.items()}
        tgt, non, acc = [], [], 0; trials = [f for f in files if f not in sum(enroll.values(), [])]
        for f in trials:
            sc = {s: float(E[f] @ c) for s, c in cent.items()}
            for s, v in sc.items():
                (tgt if s == spk[f] else non).append(v)
            acc += max(sc, key=sc.get) == spk[f]
        print(f"{cond:6s} {name:10s} EER {eer(np.array(tgt), np.array(non))*100:5.1f}%  верно узнан {acc/len(trials)*100:5.1f}%  "
              f"({(time.time()-t0)/len(files)*1000:.0f} мс/фраза)", flush=True)
