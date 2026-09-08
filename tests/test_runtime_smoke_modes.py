import unittest
from pathlib import Path

from butler.config import load_settings


class RuntimeSmokeModeTests(unittest.TestCase):
    def test_every_enabled_profile_has_direct_smoke_mode(self):
        settings = load_settings()
        for role in settings.model_roles():
            profile = settings.model(role)
            if not profile.enabled:
                continue
            with self.subTest(role=role):
                mode = settings.runtime_smoke_request_mode(role)
                self.assertFalse(mode.enable_thinking)
                self.assertGreaterEqual(mode.max_tokens, 64)

    def test_runtime_smokes_do_not_select_conversation_mode(self):
        root = Path(__file__).resolve().parents[1]
        for filename in ("test_active_model.py", "test_model_cancellation.py"):
            source = (root / "scripts" / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("settings.runtime_smoke_request_mode(state.role)", source)
                self.assertNotIn("assistant_request_mode(state.role)", source)
