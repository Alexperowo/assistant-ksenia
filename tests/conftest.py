"""Общие настройки тестов: без сети, без железа, без моделей.

Запуск: python -m pytest tests  (зависимости — requirements-test.txt)
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("core", "voice-in", "pwa"):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

# voice-in грузит Whisper при импорте модуля faster_whisper; в тестах модель не нужна
if "faster_whisper" not in sys.modules:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        fw = types.ModuleType("faster_whisper")
        fw.WhisperModel = object
        sys.modules["faster_whisper"] = fw

if "soundfile" not in sys.modules:
    try:
        import soundfile  # noqa: F401
    except ImportError:
        sf = types.ModuleType("soundfile")
        sf.write = lambda *a, **k: None
        sf.read = lambda *a, **k: (None, 16000)
        sys.modules["soundfile"] = sf
