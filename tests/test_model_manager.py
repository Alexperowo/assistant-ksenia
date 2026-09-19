import unittest
import hashlib
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

from butler.config import load_settings, reasoning_arguments
from butler.model_manager import (
    ModelManager,
    ModelManagerError,
    ModelResidencyCoordinator,
    ResidentModelPool,
    ResidencyLease,
    RuntimeState,
)


class ModelManagerTests(unittest.TestCase):
    def _coordinator(self, *, isolate_resident_vram: bool = False) -> ModelResidencyCoordinator:
        coordinator = ModelResidencyCoordinator(load_settings())
        coordinator.isolate_resident_vram = isolate_resident_vram
        return coordinator

    def test_residency_coordinator_stops_primary_before_starting_fast_pool(self):
        coordinator = self._coordinator(isolate_resident_vram=False)
        primary_state = RuntimeState(
            pid=20,
            role="generalist",
            executable="llama-server.exe",
            model="main.gguf",
            started_at="2026-08-31T00:00:00+00:00",
        )
        resident_states = {"ui_butler": object(), "research_fast": object()}
        call_order = []
        with (
            patch.object(
                coordinator.primary, "running_state", return_value=primary_state
            ),
            patch.object(
                coordinator.primary,
                "stop",
                side_effect=lambda: call_order.append("primary_stop") or True,
            ),
            patch.object(coordinator.primary, "_port_open", return_value=False),
            patch.object(
                coordinator.residents,
                "start_all",
                side_effect=lambda: call_order.append("residents_start")
                or resident_states,
            ),
        ):
            self.assertEqual(coordinator.activate_residents(), resident_states)

        self.assertEqual(call_order, ["primary_stop", "residents_start"])

    def test_residency_coordinator_restores_primary_if_fast_pool_fails(self):
        coordinator = self._coordinator(isolate_resident_vram=False)
        primary_state = RuntimeState(
            pid=20,
            role="generalist",
            executable="llama-server.exe",
            model="main.gguf",
            started_at="2026-08-31T00:00:00+00:00",
        )
        with (
            patch.object(coordinator.primary, "running_state", return_value=primary_state),
            patch.object(coordinator.primary, "stop", return_value=True),
            patch.object(coordinator.primary, "_port_open", return_value=False),
            patch.object(coordinator.primary, "start") as restart,
            patch.object(
                coordinator.residents,
                "start_all",
                side_effect=ModelManagerError("resident start failed"),
            ),
        ):
            with self.assertRaisesRegex(ModelManagerError, "resident start failed"):
                coordinator.activate_residents()

        restart.assert_called_once_with("generalist")

    def test_residency_coordinator_rolls_back_partial_suspend(self):
        coordinator = self._coordinator(isolate_resident_vram=False)
        ui_manager = Mock()
        ui_manager.running_state.return_value = object()
        ui_manager.stop.return_value = True
        ui_manager._port_open.return_value = False
        research_manager = Mock()
        research_manager.running_state.return_value = object()
        research_manager.stop.return_value = True
        research_manager._port_open.return_value = True
        managers = {"ui_butler": ui_manager, "research_fast": research_manager}
        with (
            patch.object(
                coordinator.residents,
                "running_states",
                return_value={"ui_butler": object(), "research_fast": object()},
            ),
            patch.object(
                coordinator.residents,
                "manager",
                side_effect=lambda role: managers[role],
            ),
        ):
            with self.assertRaisesRegex(ModelManagerError, "не освободил локальный порт"):
                coordinator.suspend_residents_for_primary()

        ui_manager.start.assert_not_called()
        research_manager.start.assert_called_once_with("research_fast")

    def test_primary_window_restores_exact_previous_resident_set(self):
        coordinator = self._coordinator(isolate_resident_vram=False)
        lease = ResidencyLease(("research_fast",))
        with (
            patch.object(
                coordinator, "suspend_residents_for_primary", return_value=lease
            ) as suspend,
            patch.object(coordinator, "restore_after_primary", return_value={}) as restore,
        ):
            with coordinator.primary_window() as active:
                self.assertEqual(active, lease)

        suspend.assert_called_once_with()
        restore.assert_called_once_with(lease)

    def test_restore_after_primary_stops_primary_before_exact_resident_set(self):
        coordinator = self._coordinator(isolate_resident_vram=False)
        lease = ResidencyLease(("research_fast",))
        primary_state = RuntimeState(
            pid=21,
            role="reasoning",
            executable="llama-server.exe",
            model="heavy.gguf",
            started_at="2026-08-31T00:00:00+00:00",
        )
        research_manager = Mock()
        research_state = object()
        research_manager.start.return_value = research_state
        call_order = []
        with (
            patch.object(
                coordinator.primary, "running_state", return_value=primary_state
            ),
            patch.object(
                coordinator.primary,
                "stop",
                side_effect=lambda: call_order.append("primary_stop") or True,
            ),
            patch.object(coordinator.primary, "_port_open", return_value=False),
            patch.object(
                coordinator.residents,
                "manager",
                side_effect=lambda role: call_order.append(f"start_{role}")
                or research_manager,
            ),
        ):
            restored = coordinator.restore_after_primary(lease)

        self.assertEqual(restored, {"research_fast": research_state})
        self.assertEqual(call_order, ["primary_stop", "start_research_fast"])
        research_manager.start.assert_called_once_with("research_fast")

    def test_activate_residents_fails_closed_for_unknown_primary_port_owner(self):
        coordinator = self._coordinator(isolate_resident_vram=False)
        with (
            patch.object(coordinator.primary, "running_state", return_value=None),
            patch.object(coordinator.primary, "_port_open", return_value=True),
            patch.object(coordinator.residents, "start_all") as start_all,
        ):
            with self.assertRaisesRegex(ModelManagerError, "не освободил локальный порт"):
                coordinator.activate_residents()

        start_all.assert_not_called()

    def test_role_factory_uses_its_declared_service_endpoint_and_state_file(self):
        settings = load_settings()
        manager = ModelManager.for_role(settings, "ui_butler")
        profile = settings.model("ui_butler")
        command = manager.build_command(profile)

        self.assertEqual(manager.service.name, "ui_fast")
        self.assertEqual(command[command.index("--port") + 1], "18082")
        self.assertEqual(manager.service.state_file.name, "state-ui-fast.json")
        self.assertIn("--reasoning-budget-message", ModelManager.for_role(settings, "research_fast").build_command(settings.model("research_fast")))

    def test_manager_rejects_profile_assigned_to_another_service(self):
        settings = load_settings()
        with self.assertRaisesRegex(ModelManagerError, "назначен сервису ui_fast"):
            ModelManager(settings).build_command(settings.model("ui_butler"))

    def test_resident_pool_rolls_back_only_models_started_in_failed_attempt(self):
        settings = load_settings()
        pool = ResidentModelPool(settings)
        first = pool.manager("ui_butler")
        second = pool.manager("research_fast")
        state = RuntimeState(
            pid=10,
            role="ui_butler",
            executable="llama-server.exe",
            model="ui.gguf",
            started_at="2026-08-31T00:00:00+00:00",
        )

        with (
            patch.object(pool, "manager", side_effect=lambda role: first if role == "ui_butler" else second),
            patch.object(first, "is_current", return_value=False),
            patch.object(first, "start", return_value=state),
            patch.object(first, "stop", return_value=True) as stop_first,
            patch.object(second, "is_current", return_value=False),
            patch.object(second, "start", side_effect=ModelManagerError("boom")),
        ):
            with self.assertRaisesRegex(ModelManagerError, "boom"):
                pool.start_all()

        stop_first.assert_called_once_with()

    def test_command_binds_to_loopback_and_profile(self):
        settings = load_settings()
        manager = ModelManager(settings)
        profile = settings.model("generalist")
        command = manager.build_command(profile)
        self.assertIn("127.0.0.1", command)
        self.assertIn("--model", command)
        self.assertIn(str(profile.model_path), command)
        self.assertIn("--ctx-size", command)
        self.assertEqual(
            command[-len(reasoning_arguments(profile.reasoning)) :],
            list(reasoning_arguments(profile.reasoning)),
        )

    def test_profile_extra_arguments_cannot_override_managed_security_flags(self):
        settings = load_settings()
        manager = ModelManager(settings)
        profile = settings.model("generalist")

        for extra_args in (
            ("--host", "0.0.0.0"),
            ("--host=0.0.0.0",),
            ("--api-key", "attacker-controlled"),
            ("--model", "different.gguf"),
            ("--ctx-size", "1"),
            ("--reasoning", "off"),
        ):
            with self.subTest(extra_args=extra_args):
                with self.assertRaises(ModelManagerError):
                    manager.build_command(replace(profile, extra_args=extra_args))

    def test_launch_signature_changes_with_context_size(self):
        settings = load_settings()
        manager = ModelManager(settings)
        profile = settings.model("generalist")
        larger = replace(profile, context_size=profile.context_size + 1024)
        self.assertNotEqual(
            manager.launch_signature(profile), manager.launch_signature(larger)
        )

    def test_deep_reasoning_adds_a_bounded_budget(self):
        settings = load_settings()
        manager = ModelManager(settings)
        deep = replace(settings.model("generalist"), reasoning="deep")
        command = manager.build_command(deep)
        position = command.index("--reasoning-budget")
        self.assertEqual(command[position + 1], "1536")

    def test_profiles_compile_declared_acceleration_and_auxiliary_assets(self):
        settings = load_settings()
        manager = ModelManager(settings)
        generalist = settings.model("generalist")
        generalist_command = manager.build_command(generalist)
        reasoning = settings.model("reasoning")
        reasoning_command = manager.build_command(reasoning)
        heavy_candidate = settings.model("heavy_candidate")
        heavy_command = manager.build_command(heavy_candidate)

        self.assertEqual(generalist.context_size, 98_304)
        self.assertEqual(
            generalist_command[generalist_command.index("--spec-type") + 1],
            "draft-dflash",
        )
        self.assertIn("--model-draft", generalist_command)
        self.assertNotIn("--ctx-size-draft", generalist_command)
        self.assertEqual(reasoning.context_size, 86_016)
        self.assertEqual(
            reasoning_command[reasoning_command.index("--spec-type") + 1],
            "draft-mtp",
        )
        self.assertIn("--mmproj", reasoning_command)
        self.assertNotIn("--model-draft", reasoning_command)
        self.assertEqual(heavy_candidate.context_size, 98_304)
        self.assertNotIn("--spec-type", heavy_command)
        self.assertNotIn("--spec-draft-n-max", heavy_command)
        self.assertNotIn("--model-draft", heavy_command)
        self.assertIn("--n-cpu-moe", heavy_command)

    def test_profile_backend_controls_executable_and_supported_cors_flags(self):
        settings = load_settings()
        manager = ModelManager(settings)
        generalist = settings.model("generalist")
        reasoning = settings.model("reasoning")

        generalist_command = manager.build_command(generalist)
        reasoning_command = manager.build_command(reasoning)

        self.assertEqual(generalist_command[0], str(generalist.backend.executable))
        self.assertEqual(reasoning_command[0], str(reasoning.backend.executable))
        self.assertNotIn("--cors-origins", generalist_command)
        self.assertNotIn("--no-cors-credentials", generalist_command)
        self.assertIn("--cors-origins", reasoning_command)
        self.assertIn("--no-cors-credentials", reasoning_command)
        for command in (generalist_command, reasoning_command):
            self.assertIn("--api-key-file", command)
            self.assertIn("127.0.0.1", command)

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
                settings.model("generalist"),
                model_path=model,
                expected_size_bytes=model.stat().st_size,
                sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
                draft_model_path=None,
                projector_path=None,
                acceleration_type="none",
            )
            with (
                patch.object(type(settings), "model", return_value=profile),
                patch.object(manager, "running_state", return_value=None),
                patch.object(manager, "_port_open", return_value=True),
                patch("butler.model_manager.subprocess.Popen") as popen,
            ):
                with self.assertRaises(ModelManagerError) as raised:
                    manager.start("generalist")
            detail = str(raised.exception).casefold()
            self.assertIn("порт локальной модели", detail)
            self.assertIn("уже занят", detail)
            popen.assert_not_called()

    def test_same_size_model_with_wrong_hash_is_rejected_before_process_start(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "llama-server.exe"
            model = root / "model.gguf"
            server.touch()
            model.write_bytes(b"tampered")
            settings = replace(
                load_settings(), llama_server=server, runtime_dir=root / "runtime"
            )
            manager = ModelManager(settings)
            profile = replace(
                settings.model("generalist"),
                model_path=model,
                expected_size_bytes=model.stat().st_size,
                sha256=hashlib.sha256(b"expected").hexdigest(),
                draft_model_path=None,
                projector_path=None,
                acceleration_type="none",
            )
            with (
                patch.object(type(settings), "model", return_value=profile),
                patch("butler.model_manager.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(ModelManagerError, "SHA-256"):
                    manager.start("generalist")
            popen.assert_not_called()

    def test_integrity_cache_avoids_rehashing_unchanged_artifact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            payload = b"pinned fixture"
            model.write_bytes(payload)
            settings = replace(load_settings(), runtime_dir=root / "runtime")
            manager = ModelManager(settings)
            expected_hash = hashlib.sha256(payload).hexdigest()

            with patch.object(
                manager, "_sha256_file", wraps=manager._sha256_file
            ) as calculate:
                for _ in range(2):
                    manager._verify_artifact_integrity(
                        role="test",
                        artifact="model",
                        path=model,
                        expected_size=len(payload),
                        expected_sha256=expected_hash,
                    )

            self.assertEqual(calculate.call_count, 1)

    def test_stale_runtime_state_is_removed_after_process_identity_check(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = replace(load_settings(), runtime_dir=root / "runtime")
            manager = ModelManager(settings)
            state = manager._read_state()
            self.assertIsNone(state)
            stale = RuntimeState(
                pid=12345,
                role="profile",
                executable=str(settings.llama_server),
                model="model.gguf",
                started_at="2026-08-24T00:00:00+00:00",
            )
            manager._write_state(stale)

            with patch("butler.model_manager.process_image_path", return_value=None):
                self.assertIsNone(manager.running_state())

            self.assertFalse(settings.state_file.exists())

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
                manager.settings.model("generalist"),
                model_path=model_path,
                expected_size_bytes=100,
                draft_model_path=None,
                projector_path=None,
                acceleration_type="none",
            )
            with (
                patch.object(type(manager.settings), "model", return_value=profile),
                patch("butler.model_manager.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(ModelManagerError, "неверный размер"):
                    manager.start("generalist")
            popen.assert_not_called()

    def test_incomplete_draft_is_rejected_before_process_start(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "llama-server.exe"
            model_path = root / "main.gguf"
            draft_path = root / "draft.gguf"
            server.touch()
            model_path.write_bytes(b"complete-main")
            draft_path.write_bytes(b"partial-draft")
            settings = replace(
                load_settings(), llama_server=server, runtime_dir=root / "runtime"
            )
            manager = ModelManager(settings)
            profile = replace(
                settings.model("generalist"),
                model_path=model_path,
                expected_size_bytes=model_path.stat().st_size,
                sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
                draft_model_path=draft_path,
                draft_expected_size_bytes=draft_path.stat().st_size + 1,
                projector_path=None,
                acceleration_type="draft-dflash",
            )
            with (
                patch.object(type(settings), "model", return_value=profile),
                patch("butler.model_manager.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(
                    ModelManagerError, "draft.*неверный размер"
                ):
                    manager.start("generalist")
            popen.assert_not_called()

    def test_build_command_applies_service_device(self):
        settings = load_settings()
        manager = ModelManager(settings, "primary")
        profile = settings.model("candidate")
        # Default without device
        cmd_default = manager.build_command(profile)
        # With device on service
        manager.service = replace(manager.service, device="CUDA1")
        cmd_with_dev = manager.build_command(profile)
        self.assertIn("--device", cmd_with_dev)
        idx = cmd_with_dev.index("--device")
        self.assertEqual(cmd_with_dev[idx + 1], "CUDA1")

        # If profile extra_args already has --device, it does not duplicate
        profile_with_dev = replace(profile, extra_args=profile.extra_args + ("--device", "CUDA0"))
        cmd_no_dup = manager.build_command(profile_with_dev)
        self.assertEqual(cmd_no_dup.count("--device"), 1)

    def test_residency_coordinator_isolated_vram_does_not_suspend_residents(self):
        coordinator = self._coordinator(isolate_resident_vram=True)
        self.assertTrue(coordinator.isolate_resident_vram)

        with (
            patch.object(coordinator.residents, "manager") as manager_mock,
            patch.object(coordinator.primary, "running_state", return_value=RuntimeState(1, "reasoning", "s.exe", "m.gguf", "")),
            patch.object(coordinator.primary, "stop") as primary_stop_mock,
        ):
            lease = coordinator.suspend_residents_for_primary()
            self.assertEqual(lease.active_resident_roles, ())
            manager_mock.assert_not_called()

            # restore does not touch residents and keeps primary resident
            restored = coordinator.restore_after_primary(lease)
            self.assertEqual(restored, {})
            primary_stop_mock.assert_not_called()

        # activate does not stop primary when isolated
        with (
            patch.object(coordinator.primary, "running_state", return_value=RuntimeState(1, "reasoning", "s.exe", "m.gguf", "")),
            patch.object(coordinator.primary, "stop") as stop_mock,
            patch.object(coordinator.residents, "start_all", return_value={"ui_butler": object()}),
        ):
            coordinator.activate_residents()
            stop_mock.assert_not_called()

    def test_residency_coordinator_isolated_vram_warms_up_primary(self):
        coordinator = self._coordinator(isolate_resident_vram=True)
        with (
            patch.object(coordinator.primary, "running_state", return_value=None),
            patch.object(coordinator.primary, "start", return_value=RuntimeState(2, "reasoning", "s.exe", "m.gguf", "")) as start_mock,
            patch.object(coordinator.residents, "start_all", return_value={"ui_butler": object()}),
        ):
            states = coordinator.activate_residents()
            self.assertIn("reasoning", states)
            start_mock.assert_called_once_with("reasoning")


    def test_compute_ready_returns_true_on_slot_processing(self):
        settings = load_settings()
        manager = ModelManager(settings, "ui_fast")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'[{"is_processing": true}]'
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp):
            self.assertTrue(manager.compute_ready())

    def test_compute_ready_returns_true_on_completion_200(self):
        settings = load_settings()
        manager = ModelManager(settings, "ui_fast")
        slots_resp = MagicMock()
        slots_resp.status = 200
        slots_resp.read.return_value = b'[{"is_processing": false}]'
        slots_resp.__enter__.return_value = slots_resp

        comp_resp = MagicMock()
        comp_resp.status = 200
        comp_resp.read.return_value = b'{"content": "."}'
        comp_resp.__enter__.return_value = comp_resp

        def fake_urlopen(req, timeout=None):
            if "slots" in req.full_url:
                return slots_resp
            return comp_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertTrue(manager.compute_ready())

    def test_compute_ready_returns_false_on_timeout(self):
        settings = load_settings()
        manager = ModelManager(settings, "ui_fast")
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            self.assertFalse(manager.compute_ready())

    def test_is_current_returns_false_when_compute_fails(self):
        settings = load_settings()
        manager = ModelManager(settings, "ui_fast")
        profile = settings.model("ui_butler")
        state = RuntimeState(
            pid=1234,
            role="ui_butler",
            executable="llama-server.exe",
            model="ui.gguf",
            started_at="2026-09-19T00:00:00Z",
            launch_signature=manager.launch_signature(profile),
        )
        with (
            patch.object(manager, "running_state", return_value=state),
            patch.object(manager, "api_ready", return_value=True),
            patch.object(manager, "compute_ready", return_value=False),
        ):
            self.assertFalse(manager.is_current("ui_butler"))

    def test_start_evicts_zombie_process_when_compute_fails(self):
        with TemporaryDirectory() as directory:
            settings = replace(load_settings(), runtime_dir=Path(directory))
            manager = ModelManager(settings, "ui_fast")
            profile = replace(
                settings.model("ui_butler"),
                expected_size_bytes=None,
                sha256=None,
                draft_model_path=None,
                projector_path=None,
            )
            zombie_state = RuntimeState(
                pid=9999,
                role="ui_butler",
                executable="llama-server.exe",
                model="ui.gguf",
                started_at="2026-09-17T00:00:00Z",
                launch_signature=manager.launch_signature(profile),
            )
            with (
                patch.object(type(settings), "model", return_value=profile),
                patch.object(manager, "running_state", return_value=zombie_state),
                patch.object(manager, "api_ready", return_value=True),
                patch.object(manager, "compute_ready", return_value=False),
                patch.object(manager, "stop", return_value=True) as stop_mock,
                patch.object(manager, "_wait_port_closed", return_value=True),
                patch.object(manager, "_port_open", return_value=False),
                patch.object(manager, "_server_for", return_value=Path("fake/llama-server.exe")),
                patch.object(manager, "_verify_artifact_integrity"),
                patch.object(Path, "is_file", return_value=True),
                patch("butler.model_manager.subprocess.Popen") as popen_mock,
            ):
                proc_instance = MagicMock()
                proc_instance.pid = 1111
                proc_instance.poll.return_value = None
                popen_mock.return_value = proc_instance
                with patch.object(manager, "api_ready", return_value=True):
                    with patch.object(manager, "compute_ready", return_value=True):
                        new_state = manager.start("ui_butler")
                        stop_mock.assert_called_once()
                        self.assertEqual(new_state.pid, 1111)


if __name__ == "__main__":
    unittest.main()
