"""Краткое содержание выпавшей части разговора (решение Александра 2026-10-12: человек помнит начало)."""
import asyncio

import core
import recap


def hist(n):
    h = []
    for i in range(n):
        h.append({"role": "user", "content": f"Реплика {i} про кота Барсика"})
        h.append({"role": "assistant", "content": f"Ответ {i}"})
    return h


def test_jump_summarizes_dropped_part_into_system(monkeypatch, tmp_path):
    monkeypatch.setattr(recap, "FILE", str(tmp_path / "recap.json"))
    got = {}

    async def fake_update(session, dropped, url, key, slot=1):
        got["n"] = len(dropped)
        recap.save("Говорили про кота Барсика.")
        return True
    monkeypatch.setattr(recap, "update", fake_update)
    ks = core.Ksenia.__new__(core.Ksenia)
    ks.history, ks.window_start, ks._recap_from, ks.session = hist(30), 0, None, None
    asyncio.run(ks.jump_with_recap(keep_n=10))
    assert got["n"] == 50 and ks.window_start == 50
    assert "Барсика" in ks.system and "Раньше в этом разговоре" in ks.system


def test_mid_turn_jump_leaves_pending_recap(monkeypatch, tmp_path):
    monkeypatch.setattr(recap, "FILE", str(tmp_path / "recap.json"))
    monkeypatch.setitem(core.CONFIG, "history_max", 20)
    ks = core.Ksenia.__new__(core.Ksenia)
    ks.history, ks.window_start, ks._recap_from, ks.session = hist(30), 0, None, None
    ks._jump()
    assert ks._recap_from == 0 and ks.window_start > 0  # выпавшее допишется в тишине


def test_failed_brain_keeps_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(recap, "FILE", str(tmp_path / "recap.json"))

    async def fail(*a, **k):
        return False
    monkeypatch.setattr(recap, "update", fail)
    ks = core.Ksenia.__new__(core.Ksenia)
    ks.history, ks.window_start, ks._recap_from, ks.session = hist(30), 0, 0, None
    asyncio.run(ks.jump_with_recap(keep_n=10))
    assert ks._recap_from == 0  # не потеряли: попробуем при следующей тишине
