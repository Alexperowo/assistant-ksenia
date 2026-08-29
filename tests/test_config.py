import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from butler.config import (
    ConfigError,
    load_settings,
    reasoning_arguments,
    response_budget_label,
    set_user_headset_control,
    set_user_model,
    set_user_reasoning,
    set_user_response_budget,
    write_user_settings,
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

    def test_invalid_live_phrase_bounds_fail_during_configuration_load(self):
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
                "server": {"host": "127.0.0.1", "port": 18080},
                "live": {
                    "enabled": True,
                    "minimum_phrase_chars": 300,
                    "maximum_phrase_chars": 20,
                },
                "models": {},
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigError, "Границы Live-фраз"):
                load_settings(root)

    def test_invalid_live_semantic_endpointing_type_fails_closed(self):
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
                "server": {"host": "127.0.0.1", "port": 18080},
                "live": {"semantic_endpointing": "yes"},
                "models": {},
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigError, "semantic_endpointing"):
                load_settings(root)

    def test_invalid_live_turn_silence_order_fails_closed(self):
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
                "server": {"host": "127.0.0.1", "port": 18080},
                "live": {
                    "turn_complete_silence_seconds": 1.0,
                    "turn_ordinary_silence_seconds": 0.5,
                },
                "models": {},
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigError, "Паузы Live-реплики"):
                load_settings(root)

    def test_invalid_confirmation_microphone_handoff_timeout_fails_closed(self):
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
                "server": {"host": "127.0.0.1", "port": 18080},
                "voice": {
                    "confirmation_microphone_handoff_timeout_seconds": "never"
                },
                "models": {},
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigError, "передачи микрофона"):
                load_settings(root)

    def test_security_booleans_reject_string_false(self):
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
                "server": {"host": "127.0.0.1", "port": 18080},
                "browser": {"active_control_enabled": "false"},
                "models": {},
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigError, "active_control_enabled"):
                load_settings(root)

    def test_model_enabled_flag_rejects_string_false(self):
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
                "server": {"host": "127.0.0.1", "port": 18080},
                "models": {
                    "dev": {
                        "label": "Dev",
                        "filename": "dev.gguf",
                        "context_size": 4096,
                        "gpu_layers": 0,
                        "enabled": "false",
                    }
                },
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigError, "enabled/experimental"):
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

    def test_path_configuration_preserves_unrelated_personal_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "user.json").write_text(
                json.dumps(
                    {
                        "voice": {"speaker": "xenia"},
                        "paths": {"model_search_dirs": ["secondary"]},
                    }
                ),
                encoding="utf-8",
            )

            write_user_settings(root, root / "engine.exe", root / "models")

            saved = json.loads(
                (root / "config" / "user.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["voice"]["speaker"], "xenia")
            self.assertEqual(saved["paths"]["model_search_dirs"], ["secondary"])
            self.assertEqual(saved["paths"]["models_dir"], str((root / "models").resolve()))

    def test_concurrent_user_config_updates_preserve_unrelated_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()

            operations = []
            for index in range(20):
                operations.extend(
                    [
                        lambda value=index: set_user_reasoning(
                            root, f"role-{value}", "brief"
                        ),
                        lambda value=index: set_user_headset_control(
                            root, f"button-{value}", consume=bool(value % 2)
                        ),
                    ]
                )
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda operation: operation(), operations))

            saved = json.loads(
                (root / "config" / "user.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(saved["models"]), 20)
        self.assertIn("headset_controls", saved)

    def test_model_path_update_migrates_legacy_override_without_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "user.json").write_text(
                json.dumps({"models": {"profile": {"path": "legacy.gguf"}}}),
                encoding="utf-8",
            )

            set_user_model(root, "profile", root / "new.gguf")

            saved = json.loads(
                (root / "config" / "user.json").read_text(encoding="utf-8")
            )
            profile = saved["models"]["profile"]
            self.assertNotIn("path", profile)
            self.assertEqual(
                profile["artifacts"]["model"]["path"],
                str((root / "new.gguf").resolve()),
            )

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

    def test_default_roles_and_execution_policy_are_safe(self):
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

            self.assertEqual(developer.primary_model, "generalist")
            self.assertEqual(developer.candidate_model, "candidate")
            self.assertTrue(developer.enabled)
            self.assertEqual(heavy_brain.primary_model, "reasoning")
            self.assertTrue(heavy_brain.enabled)
            self.assertEqual(settings.default_role, "generalist")
            execution = settings.raw["developer"]["execution"]
            self.assertEqual(execution["backend"], "disabled")
            self.assertEqual(execution["unsafe_host_acknowledgement"], "")
            self.assertEqual(settings.raw["voice"]["speaker"], "xenia")
            live = settings.raw["live"]
            self.assertFalse(live["enabled"])
            self.assertLessEqual(
                live["minimum_phrase_chars"], live["maximum_phrase_chars"]
            )

    def test_candidate_is_reproducible_conservative_and_disabled(self):
        root = Path(__file__).resolve().parents[1]
        defaults = json.loads(
            (root / "config" / "default.json").read_text(encoding="utf-8")
        )

        candidate = defaults["models"]["candidate"]
        artifact = candidate["artifacts"]["model"]

        self.assertEqual(
            artifact["source_repo"],
            "conFIGur8tor/ornith15-35b-a3b-apex-mtp-fixed",
        )
        self.assertEqual(
            artifact["source_revision"],
            "63518f71da021c5d2f1dc5fa1dfa7fa74437d6aa",
        )
        self.assertEqual(
            artifact["source_filename"],
            "ornith15-trained-head.gguf",
        )
        self.assertEqual(artifact["expected_size_bytes"], 17_437_861_152)
        self.assertEqual(
            artifact["sha256"],
            "344925ae3f65a57a55c1db1acaf52e7ca49aaf5e0b845b797964e73106f6e340",
        )
        self.assertEqual(candidate["context_size"], 16_384)
        self.assertEqual(candidate["acceleration"]["type"], "draft-mtp")
        self.assertEqual(candidate["acceleration"]["max_tokens"], 2)
        self.assertFalse(candidate["enabled"])
        self.assertTrue(candidate["experimental"])

    def test_working_profiles_are_generic_pinned_and_have_declared_capabilities(self):
        settings = load_settings()

        self.assertEqual(settings.model_roles(), ("generalist", "reasoning", "candidate"))
        self.assertEqual(settings.agent_max_steps, 8)
        self.assertEqual(settings.developer_max_steps, 24)
        self.assertGreaterEqual(
            settings.raw["agent"]["max_tool_calls_total"],
            settings.developer_max_steps,
        )
        generalist = settings.model("generalist")
        reasoning = settings.model("reasoning")

        self.assertEqual(generalist.acceleration_type, "draft-dflash")
        self.assertIsNotNone(generalist.draft_model_path)
        self.assertEqual(reasoning.acceleration_type, "draft-mtp")
        self.assertIsNotNone(reasoning.projector_path)
        for profile_name in settings.model_roles():
            artifacts = settings.raw["models"][profile_name]["artifacts"]
            for artifact in artifacts.values():
                self.assertEqual(len(artifact["source_revision"]), 40)
                self.assertEqual(len(artifact["sha256"]), 64)
                self.assertNotIn(":\\", artifact["filename"])

    def test_declared_draft_acceleration_requires_a_draft_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            defaults = json.loads(
                (Path(__file__).resolve().parents[1] / "config" / "default.json").read_text(
                    encoding="utf-8"
                )
            )
            defaults["models"]["generalist"]["artifacts"].pop("draft")
            (root / "config" / "default.json").write_text(
                json.dumps(defaults), encoding="utf-8"
            )

            with self.assertRaisesRegex(ConfigError, "требует артефакт draft"):
                load_settings(root)

    def test_model_search_directory_resolves_nested_artifact_without_code_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            artifact = root / "alternate" / "nested" / "brain.gguf"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"fixture")
            default = {
                "assistant": {"name": "Тест", "default_role": "profile"},
                "paths": {
                    "llama_server": "server.exe",
                    "models_dir": "models",
                    "model_search_dirs": ["alternate"],
                    "runtime_dir": "runtime",
                },
                "server": {"host": "127.0.0.1", "port": 18080},
                "models": {
                    "profile": {
                        "label": "Profile",
                        "artifacts": {"model": {"filename": "brain.gguf"}},
                        "context_size": 4096,
                        "gpu_layers": 0,
                    }
                },
            }
            (root / "config" / "default.json").write_text(
                json.dumps(default), encoding="utf-8"
            )

            settings = load_settings(root)

            self.assertEqual(settings.model("profile").model_path, artifact.resolve())


if __name__ == "__main__":
    unittest.main()
