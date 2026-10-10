exec(open("t3_step.py").read().split("# (a)")[0])
for text in ("Что?", "Который час?"):
    clock_holder = {}
    orig = Clock.__init__
    a, info = run(pcm(zeros(1.0), speech(0.4), zeros(5)), text=text)
    print(f"step: {text!r}: utterance ended {voice_in.time.t-1000-1.4:.2f} s after he stopped talking")
