exec(open("t2_segmenter.py").read().split("def end_time")[0])
for dur in (0.2, 0.3, 0.4):
    seg = voice_in.LiveSegmenter(dict(voice_in.CONFIG)); seg.ctx.update({"asked": True})
    for f in frames(zeros(1.0), speech(dur), zeros(3)):
        ev = seg.push(f)
        if ev == "check_fast": ev = seg.decide(1.0, "Да.", fast=True)
        elif ev == "check": ev = seg.decide(0.99, "Да.")
        if ev == "end":
            pcm, info = seg.utterance(); print(f"live 'да' {dur}s -> utterance pcm {len(pcm)/16000:.2f}s; voiceprint embeds only if >= 0.60s"); break
