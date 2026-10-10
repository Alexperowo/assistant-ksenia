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


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_writes_to_real_data(monkeypatch, tmp_path):
    """Тесты не пишут в настоящие журналы Ксении (data/): живой тест журнала решений засорялся прогонами тестов."""
    try:
        import live_intent
        monkeypatch.setattr(live_intent, "DECISIONS_FILE", str(tmp_path / "live_decisions.jsonl"))
    except ImportError:
        pass
    try:
        import core
        monkeypatch.setattr(core, "NOTES_FILE", str(tmp_path / "agent_notes.md"))
        # ночью Ксения тише (night_gain) — тесты озвучки не должны зависеть от времени запуска
        monkeypatch.setitem(core.CONFIG, "night_from", 24)
        monkeypatch.setitem(core.CONFIG, "night_to", 0)
    except ImportError:
        pass
    try:
        import voice_in
        monkeypatch.setattr(voice_in.Mood, "FILE", str(tmp_path / "voice_baseline.json"))
        monkeypatch.setattr(voice_in, "mood", voice_in.Mood())
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch):
    """Тесты не перезапускают настоящие службы и не зовут настоящий запасной голос (2026-10-10: тест сбоя голоса
    перезапустил настоящий ksenia-voice-out)."""
    try:
        import core
    except Exception:
        return
    restarted = []
    if hasattr(core, "SPOKEN"):
        core.SPOKEN.clear()  # «что Ксения недавно говорила» — своё в каждом тесте (иначе чужое «Привет» — эхо)
    monkeypatch.setattr(core, "voice_out_broken", lambda: restarted.append("ksenia-voice-out"), raising=False)
    monkeypatch.setattr(core, "fallback_pcm", lambda text: b"", raising=False)
    monkeypatch.setattr(core, "restart_unit", lambda unit, why, every_s=180: restarted.append(unit) or True,
                        raising=False)
    return restarted


@pytest.fixture(autouse=True)
def _fresh_confirm_context():
    """Что сказал Александр (confirm.CONTEXT) — своё в каждом тесте, иначе «отмени…» одного теста разрешало бы
    действие в другом."""
    try:
        from tools import confirm
    except Exception:
        yield
        return
    confirm.CONTEXT.update({"user_text": "", "internal": False, "affirmative": False, "last_said": ""})
    yield
    confirm.CONTEXT.update({"user_text": "", "internal": False, "affirmative": False, "last_said": ""})
