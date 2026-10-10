import sys; sys.path.insert(0, "/home/user/assistant-ksenia/voice-in"); sys.path.insert(0, "/home/user/assistant-ksenia/tests")
import conftest  # stubs faster_whisper
import voice_in
cfg = dict(voice_in.CONFIG)
for text, ctx in [("Что?", {}), ("Как?", {}), ("Где?", {}), ("Что это?", {}), ("Кто это?", {}), ("А ты?", {}),
                  ("Как у тебя?", {}), ("Почему так?", {}), ("И что?", {}), ("Давай.", {"asked": True}),
                  ("Да, давай!", {"asked": True}), ("Ну давай.", {"asked": True}), ("Расскажи ещё.", {}),
                  ("Который час?", {}), ("Да.", {"asked": True})]:
    print(f"{text!r:22} ctx={ctx}  fast={voice_in.turn_policy(1.0, text, ctx, cfg, fast=True)}  "
          f"regular={voice_in.turn_policy(0.99, text, ctx, cfg)}  step_mode_hang={voice_in.hanging(text)}")
