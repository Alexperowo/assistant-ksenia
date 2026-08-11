import json
import tempfile
import unittest
from pathlib import Path

from butler.config import (
    ConfigError,
    load_settings,
    reasoning_arguments,
    response_budget_label,
    set_user_headset_control,
    set_user_reasoning,
    set_user_response_budget,
)


class ConfigTests(unittest.TestCase):
    def test_model_api_cannot_be_exposed_by_user_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            default = {
                "assistant": {"name": "Тест", "default_role": "dev"},
                "paths": {
                    "llama_server": "server.exe",
                    "models_dir": "models",
                    "runtime_dir": "runtime",
                },
                "server": {"host": "0.0.0.0", "port": 18080},
                "models": {},
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )
            with self.assertRaises(ConfigError):
                load_settings(root)

    def test_user_config_deep_merges_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            default = {
                "assistant": {"name": "Тест", "default_role": "dev"},
                "paths": {
                    "llama_server": "server.exe",
                    "models_dir": "models",
                    "runtime_dir": "runtime",
                },
                "server": {"host": "127.0.0.1", "port": 1234},
                "models": {
                    "dev": {
                        "label": "Dev",
                        "filename": "dev.gguf",
                        "context_size": 4096,
                        "gpu_layers": 1,
                    }
                },
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )
            (root / "config" / "user.json").write_text(
                json.dumps({"paths": {"models_dir": "other-models"}}), encoding="utf-8"
            )
            settings = load_settings(root)
            self.assertEqual(settings.port, 1234)
            self.assertEqual(settings.models_dir, (root / "other-models").resolve())
            self.assertEqual(settings.llama_server, (root / "server.exe").resolve())

    def test_reasoning_level_is_saved_without_replacing_model_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "user.json").write_text(
                json.dumps({"models": {"developer": {"path": "D:/AI/model.gguf"}}}),
                encoding="utf-8",
            )

            set_user_reasoning(root, "developer", "deep")

            saved = json.loads((root / "config" / "user.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["models"]["developer"]["path"], "D:/AI/model.gguf")
            self.assertEqual(saved["models"]["developer"]["reasoning"], "deep")
            self.assertEqual(
                reasoning_arguments("brief"),
                ("--reasoning", "on", "--reasoning-budget", "256"),
            )

    def test_response_budget_updates_agent_and_planner_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()

            set_user_response_budget(root, 8192)

            saved = json.loads((root / "config" / "user.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["generation"]["max_tokens"], 8192)
            self.assertEqual(saved["routing"]["research_turn_max_tokens"], 2048)
            self.assertEqual(saved["routing"]["plan_max_tokens"], 8192)
            self.assertEqual(response_budget_label(8192), "подробно")

    def test_headset_control_update_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "user.json").write_text(
                json.dumps({"voice": {"speaker": "xenia"}}), encoding="utf-8"
            )

            set_user_headset_control(root, "play_pause")

            saved = json.loads(
                (root / "config" / "user.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["voice"]["speaker"], "xenia")
            self.assertTrue(saved["headset_controls"]["enabled"])
            self.assertEqual(
                saved["headset_controls"]["activation_button"], "play_pause"
            )

    def test_capability_roles_separate_purpose_from_model_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            source = Path(__file__).resolve().parents[1] / "config" / "default.json"
            (root / "config" / "default.json").write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
            settings = load_settings(root)

            developer = settings.capability_role("developer")
            heavy_brain = settings.capability_role("heavy_brain")

            self.assertEqual(developer.primary_model, "developer")
            self.assertEqual(developer.candidate_model, "developer_qwopus")
            self.assertTrue(developer.enabled)
            self.assertIsNone(heavy_brain.primary_model)
            self.assertFalse(heavy_brain.enabled)


if __name__ == "__main__":
    unittest.main()
