import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from butler.config import load_settings, reasoning_arguments
from butler.model_manager import ModelManager, ModelManagerError


class ModelManagerTests(unittest.TestCase):
    def test_command_binds_to_loopback_and_profile(self):
        settings = load_settings()
        manager = ModelManager(settings)
        profile = settings.model("developer")
        command = manager.build_command(profile)
        self.assertIn("127.0.0.1", command)
        self.assertIn("--model", command)
        self.assertIn(str(profile.model_path), command)
        self.assertIn("--ctx-size", command)
        self.assertEqual(
            command[-len(reasoning_arguments(profile.reasoning)) :],
            list(reasoning_arguments(profile.reasoning)),
        )

    def test_launch_signature_changes_with_context_size(self):
        settings = load_settings()
        manager = ModelManager(settings)
        profile = settings.model("developer")
        larger = replace(profile, context_size=profile.context_size + 1024)
        self.assertNotEqual(
            manager.launch_signature(profile), manager.launch_signature(larger)
        )

    def test_deep_reasoning_adds_a_bounded_budget(self):
        settings = load_settings()
        manager = ModelManager(settings)
        deep = replace(settings.model("developer"), reasoning="deep")
        command = manager.build_command(deep)
        position = command.index("--reasoning-budget")
        self.assertEqual(command[position + 1], "1536")

    def test_qwopus_profile_uses_64k_asymmetric_kv_and_bounded_mtp(self):
        settings = load_settings()
        manager = ModelManager(settings)
        profile = settings.model("developer_qwopus")
        command = manager.build_command(profile)

        self.assertEqual(profile.context_size, 65_536)
        self.assertFalse(profile.experimental)
        self.assertEqual(command[command.index("--cache-type-k") + 1], "q8_0")
        self.assertEqual(command[command.index("--cache-type-v") + 1], "q5_0")
        self.assertEqual(command[command.index("--spec-type") + 1], "draft-mtp")
        self.assertEqual(command[command.index("--spec-draft-n-max") + 1], "2")

    def test_unknown_port_owner_is_never_treated_as_new_model(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "llama-server.exe"
            model = root / "model.gguf"
            server.touch()
            model.write_bytes(b"valid-enough-for-this-guard")
            settings = replace(
                load_settings(), llama_server=server, runtime_dir=root / "runtime"
            )
            manager = ModelManager(settings)
            profile = replace(
                settings.model("developer"),
                model_path=model,
                expected_size_bytes=model.stat().st_size,
            )
            with (
                patch.object(type(settings), "model", return_value=profile),
                patch.object(manager, "running_state", return_value=None),
                patch.object(manager, "_port_open", return_value=True),
                patch("butler.model_manager.subprocess.Popen") as popen,
            ):
                with self.assertRaises(ModelManagerError) as raised:
                    manager.start("developer")
            detail = str(raised.exception).casefold()
            self.assertIn("порт локальной модели", detail)
            self.assertIn("уже занят", detail)
            popen.assert_not_called()

    def test_port_release_wait_is_bounded_and_observes_close(self):
        manager = ModelManager(load_settings())
        with (
            patch.object(manager, "_port_open", side_effect=[True, True, False]),
            patch("butler.model_manager.time.sleep") as sleep,
        ):
            self.assertTrue(manager._wait_port_closed(timeout=1))
        self.assertEqual(sleep.call_count, 2)

    def test_incomplete_model_is_rejected_before_process_start(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "llama-server.exe"
            server.touch()
            settings = replace(
                load_settings(), llama_server=server, runtime_dir=root / "runtime"
            )
            manager = ModelManager(settings)
            model_path = root / "candidate.gguf"
            model_path.write_bytes(b"incomplete")
            profile = replace(
                manager.settings.model("developer_qwopus"),
                model_path=model_path,
                expected_size_bytes=100,
            )
            with (
                patch.object(type(manager.settings), "model", return_value=profile),
                patch("butler.model_manager.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(ModelManagerError, "неверный размер"):
                    manager.start("developer_qwopus")
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
