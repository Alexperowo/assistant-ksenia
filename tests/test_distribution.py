from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_release  # noqa: E402
import maintenance  # noqa: E402


class DistributionTests(unittest.TestCase):
    def test_engine_version_parser_accepts_supported_upstream_formats(self):
        self.assertTrue(
            maintenance.engine_version_matches(
                "version: 10241 (9bd4c09ea)", "10241", "9bd4c09ea"
            )
        )
        self.assertTrue(
            maintenance.engine_version_matches(
                "version: 0.3.0-dev (build 10621, commit c1d0e7a00)",
                "10621",
                "c1d0e7a00",
            )
        )
        self.assertFalse(
            maintenance.engine_version_matches(
                "version: 0.3.0-dev (build 10621, commit deadbeef00)",
                "10621",
                "c1d0e7a00",
            )
        )

    def test_source_tree_matches_machine_readable_release_contract(self):
        report = validate_release.validate(ROOT, package_mode=False)
        self.assertTrue(report["ok"])
        self.assertFalse(report["package"])
        self.assertGreaterEqual(report["required_file_count"], 30)
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            workflow,
        )
        self.assertIn(
            "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
            workflow,
        )
        self.assertNotRegex(workflow, r"uses:\s+actions/(?:checkout|setup-python)@v\d")

    def test_release_version_matches_pyproject(self):
        manifest = json.loads(
            (ROOT / "config" / "release-manifest.json").read_text(encoding="utf-8")
        )
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{manifest["project"]["version"]}"', pyproject)

    def test_archive_contract_excludes_models_personal_state_and_secrets(self):
        manifest = json.loads(
            (ROOT / "config" / "release-manifest.json").read_text(encoding="utf-8")
        )
        package = manifest["package"]
        self.assertIn("config/user.json", package["excluded_files"])
        self.assertIn("runtime", package["excluded_path_parts"])
        self.assertIn(".gguf", package["forbidden_extensions"])
        self.assertIn(".pt", package["forbidden_extensions"])
        self.assertIn(".onnx", package["forbidden_extensions"])
        self.assertIn(".key", package["forbidden_extensions"])
        self.assertFalse(manifest["models"]["bundled"])
        defaults = json.loads(
            (ROOT / "config" / "default.json").read_text(encoding="utf-8")
        )
        serialized = json.dumps(defaults, ensure_ascii=False)
        self.assertNotRegex(serialized, r'"[A-Za-z]:[\\/]')
        self.assertEqual(defaults["paths"]["model_search_dirs"], [])

    def test_executable_code_has_no_model_family_or_machine_path_hardcoding(self):
        runtime_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "src" / "butler").glob("*.py"))
        ).casefold()
        script_source = "\n".join(
            path.read_text(encoding="utf-8-sig")
            for path in sorted(SCRIPTS.iterdir())
            if path.suffix.casefold() in {".py", ".ps1"}
        ).casefold()
        for family in ("gemma", "qwopus", "laguna", "ornith", "qwen"):
            self.assertNotIn(family, runtime_source)
            self.assertNotIn(family, script_source)
        # A drive literal starts at a token boundary; the naive form also
        # mistakes the trailing ``p:/`` in ``http://`` for a Windows path.
        self.assertNotRegex(runtime_source, r"(?<![a-z])[a-z]:[\\/]")
        self.assertNotRegex(script_source, r"(?<![a-z])[a-z]:[\\/]")

    def test_runtime_python_resolver_works_in_windows_powershell(self):
        for script in sorted(SCRIPTS.glob("*.ps1")):
            payload = script.read_bytes()
            if any(byte >= 128 for byte in payload):
                self.assertTrue(
                    payload.startswith(b"\xef\xbb\xbf"),
                    f"Non-ASCII PowerShell must have UTF-8 BOM: {script.name}",
                )
        environment = {
            **os.environ,
            "KSENIA_TEST_ROOT": str(ROOT),
            "KSENIA_TEST_PYTHON": sys.executable,
        }
        command = (
            "& { "
            "$errors=@(); "
            "Get-ChildItem -LiteralPath (Join-Path $env:KSENIA_TEST_ROOT 'scripts') "
            "-Filter '*.ps1' | ForEach-Object { "
            "$tokens=$null; $parseErrors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$_.FullName,[ref]$tokens,[ref]$parseErrors) | Out-Null; "
            "if($parseErrors.Count){$errors += $parseErrors} }; "
            "if($errors.Count){throw $errors[0].Message}; "
            ". (Join-Path $env:KSENIA_TEST_ROOT 'scripts\\runtime-paths.ps1'); "
            "Resolve-KseniaPython -ProjectRoot $env:KSENIA_TEST_ROOT "
            "-ExplicitPath $env:KSENIA_TEST_PYTHON "
            "}"
        )

        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(Path(completed.stdout.strip()).resolve(), Path(sys.executable).resolve())

    def test_forbidden_path_guard_handles_nested_and_case_variants(self):
        rules = {
            "excluded_files": ["config/user.json"],
            "excluded_path_parts": ["runtime", "tools/llama.cpp", "tools/llama.poolside"],
            "forbidden_extensions": [".gguf", ".key"],
        }
        self.assertTrue(validate_release._path_is_forbidden("CONFIG/user.json", rules))
        self.assertTrue(validate_release._path_is_forbidden("runtime/logs/a.json", rules))
        self.assertTrue(validate_release._path_is_forbidden("tools/llama.cpp/x.dll", rules))
        self.assertTrue(validate_release._path_is_forbidden("tools/llama.poolside/x.dll", rules))
        self.assertTrue(validate_release._path_is_forbidden("models/BRAIN.GGUF", rules))
        self.assertFalse(validate_release._path_is_forbidden("src/butler/agent.py", rules))

    def test_package_hash_manifest_detects_content_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "README.md"
            payload.write_text("чистый пакет", encoding="utf-8")
            package_manifest = {
                "files": [
                    {
                        "path": "README.md",
                        "size_bytes": payload.stat().st_size,
                        "sha256": validate_release._sha256(payload),
                    }
                ]
            }
            (root / "PACKAGE-MANIFEST.json").write_text(
                json.dumps(package_manifest), encoding="utf-8"
            )
            rules = {
                "package": {
                    "excluded_files": ["config/user.json"],
                    "excluded_path_parts": ["runtime"],
                    "forbidden_extensions": [".gguf"],
                }
            }
            self.assertEqual(validate_release._validate_package(root, rules), 1)
            payload.write_text("подменённый пакет", encoding="utf-8")
            with self.assertRaises(validate_release.ValidationError):
                validate_release._validate_package(root, rules)

    def test_archive_validation_rejects_traversal_before_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "malicious.zip"
            extraction = root / "extract"
            outside = root / "outside.txt"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("Ksenia/PACKAGE-MANIFEST.json", '{"files": []}')
                handle.writestr("../outside.txt", "must-not-extract")

            with self.assertRaises(validate_release.ValidationError):
                validate_release.validate_archive(archive, extraction)

            self.assertFalse(outside.exists())
            self.assertFalse(extraction.exists())

    def test_archive_validation_rejects_symbolic_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "symlink.zip"
            extraction = root / "extract"
            link = zipfile.ZipInfo("Ksenia/link")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("Ksenia/PACKAGE-MANIFEST.json", '{"files": []}')
                handle.writestr(link, "../../outside.txt")

            with self.assertRaises(validate_release.ValidationError):
                validate_release.validate_archive(archive, extraction)

            self.assertFalse(extraction.exists())

    def test_release_verifier_runs_only_the_trusted_local_validator(self):
        verifier = (SCRIPTS / "verify-release.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("Join-Path $PSScriptRoot 'validate_release.py'", verifier)
        self.assertNotIn("Join-Path $packageRoot 'scripts\\validate_release.py'", verifier)
        self.assertIn("--archive", verifier)

    def test_release_builder_refuses_dirty_git_tree_by_default(self):
        builder = (SCRIPTS / "build-release.ps1").read_text(encoding="utf-8-sig")

        self.assertIn("[switch]$AllowDirtySource", builder)
        self.assertIn("status --porcelain=v1 --untracked-files=all", builder)
        self.assertIn("-not $AllowDirtySource", builder)

    def test_hardware_profiles_are_conservative_and_do_not_select_models(self):
        value = json.loads(
            (ROOT / "config" / "hardware-profiles.json").read_text(encoding="utf-8")
        )
        profiles = value["profiles"]
        self.assertEqual(profiles[0]["recommended_context"], 8192)
        self.assertEqual(profiles[-1]["recommended_context"], 65536)
        self.assertTrue(all("model" not in item for item in profiles))
        self.assertTrue(all(item["cache_type_k"] == "q8_0" for item in profiles))
        self.assertTrue(all(item["cache_type_v"] == "q5_0" for item in profiles))

    def test_installer_uses_exact_locks_and_never_downloads_llm(self):
        installer = (SCRIPTS / "install-runtime.ps1").read_text(encoding="utf-8-sig")
        runtime_lock = (ROOT / "requirements" / "runtime.lock.txt").read_text(
            encoding="utf-8"
        )
        runtime_assets = json.loads(
            (ROOT / "config" / "runtime-assets.lock.json").read_text(encoding="utf-8")
        )
        torch_lock_path = ROOT / runtime_assets["torch"]["requirements"]
        torch_lock = torch_lock_path.read_text(encoding="utf-8")
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("runtime.lock.txt", installer)
        self.assertIn("$assetLock.torch.requirements", installer)
        self.assertNotIn("download.pytorch.org/whl/cu", installer)
        setuptools_line = next(
            line for line in runtime_lock.splitlines() if line.startswith("setuptools==")
        )
        self.assertEqual(pyproject["build-system"]["requires"], [setuptools_line])
        setuptools_version = tuple(int(part) for part in setuptools_line.split("==", 1)[1].split("."))
        pip_version = tuple(int(part) for part in runtime_assets["python"]["pip"].split("."))
        self.assertGreaterEqual(setuptools_version, (84, 0, 0))
        self.assertGreaterEqual(pip_version, (26, 2, 1))
        self.assertEqual(
            torch_lock.strip().splitlines()[-1],
            f'torch=={runtime_assets["packages"]["torch"]}',
        )
        self.assertTrue(runtime_assets["torch"]["index_url"].startswith("https://download.pytorch.org/whl/"))
        self.assertIn("[string]$ModelStorageRoot", installer)
        self.assertIn("pip==$", installer)
        self.assertIn("chrome-win64", installer)
        self.assertIn("playwright install --no-shell chromium", installer)
        self.assertNotIn("sync_playwright", installer)
        self.assertNotIn("pip install --upgrade pip", installer)
        self.assertNotIn("snapshot_download(\n    repo_id=os.environ[\"KSENIA_LLM", installer)
        self.assertIn("[string]$AllowedRoot", installer)
        self.assertIn("Move-ToInstallerQuarantine $wakeModelPath -AllowedRoot $voiceRoot", installer)
        self.assertIn("$wakeModelInsideManagedRoot", installer)
        self.assertEqual(
            runtime_assets["silero_tts"]["sha256"],
            "7BA04D42340FE0398042EED2E0D12D62E23096D626B1B9FEFF4DCB1309197AB4",
        )
        voice_worker = (SCRIPTS / "voice_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("silero_tts(", voice_worker)
        self.assertNotIn("download_url_to_file", voice_worker)
        self.assertIn("PackageImporter", voice_worker)
        self.assertIn(
            "--model-path",
            (ROOT / "src" / "butler" / "speech.py").read_text(encoding="utf-8"),
        )

    def test_update_is_locked_staged_audited_and_reversible(self):
        updater = (SCRIPTS / "update.ps1").read_text(encoding="utf-8-sig")
        engine = (SCRIPTS / "install-llama.ps1").read_text(encoding="utf-8-sig")
        local_backend = (SCRIPTS / "install-local-backend.ps1").read_text(encoding="utf-8-sig")
        rollback = (SCRIPTS / "rollback-update.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("[switch]$CheckOnly", updater)
        self.assertIn("engine.lock.json", (SCRIPTS / "maintenance.py").read_text(encoding="utf-8"))
        self.assertIn("rollback-update.ps1", updater)
        self.assertIn("check.ps1", updater)
        self.assertIn(".llama-stage-", engine)
        self.assertIn("Get-FileHash -Algorithm SHA256", engine)
        self.assertIn("runtime_files", local_backend)
        self.assertIn("Get-FileHash -Algorithm SHA256", local_backend)
        self.assertIn(".backend-stage-", local_backend)
        self.assertIn("rollback-displaced-engine-", rollback)
        self.assertIn("engine_sha256_before", updater)
        self.assertIn("engine_version_before", updater)
        self.assertIn("python_before", updater)
        self.assertIn("Get-FileHash -Algorithm SHA256", rollback)
        self.assertIn("Compare-Object", rollback)
        self.assertNotIn("$afterStatus.engine.matches", rollback)
        self.assertNotIn("$afterStatus.python.matches", rollback)
        self.assertNotIn("github.com/ggml-org/llama.cpp/releases/latest", updater + engine)

    def test_public_cmd_entrypoints_propagate_failures(self):
        manifest = json.loads(
            (ROOT / "config" / "release-manifest.json").read_text(encoding="utf-8")
        )
        for relative in manifest["entry_points"].values():
            text = (ROOT / relative).read_text(encoding="utf-8-sig").casefold()
            self.assertIn("errorlevel 1", text, relative)
            self.assertIn("exit /b 1", text, relative)

    def test_desktop_shortcut_contract_is_complete_and_verified(self):
        expected = {
            "Ксения — инструкция Александра": ("OPEN-ALEXANDER-GUIDE.cmd", ""),
            "Ксения — запрос для поиска моделей": ("OPEN-MODEL-SEARCH-REQUEST.cmd", ""),
            "Ксения — НАЧАТЬ РАЗГОВОР": ("START-VOICE.cmd", "CTRL+ALT+K"),
            "Ксения — ОСТАНОВИТЬ ГОЛОС": ("STOP-VOICE.cmd", "CTRL+ALT+S"),
            "Ксения — помощь и управление": ("START-BUTLER.cmd", "CTRL+ALT+U"),
            "Ксения — ДОВЕРЕННАЯ ЗАДАЧА": ("TRUST-NEXT-TASK.cmd", "CTRL+ALT+D"),
            "Ксения — локальная сеть": ("START-LAN.cmd", ""),
            "Ксения — проверка микрофона": ("TEST-MICROPHONE.cmd", "CTRL+ALT+M"),
            "Ксения — проверка активации": ("TEST-WAKE-WORD.cmd", ""),
            "Ксения — проверка голоса": ("TEST-VOICE.cmd", ""),
            "Ксения — проверка кнопки наушников": ("TEST-HEADSET-CONTROLS.cmd", ""),
            "Ксения — список микрофонов": ("AUDIO-DEVICES.cmd", ""),
            "Ксения — полный аудит": ("AUDIT.cmd", ""),
            "Ксения — вход в сайты": ("BROWSER-PROFILE.cmd", ""),
        }
        installer = (SCRIPTS / "install-shortcuts.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertEqual(installer.count("@{ Name = 'Ксения —"), len(expected))
        self.assertIn("Ярлык не прошёл проверку после сохранения", installer)
        self.assertIn("$link.Hotkey = if", installer)
        for name, (target, hotkey) in expected.items():
            self.assertIn(f"Name = '{name}'", installer)
            self.assertIn(f"Target = '{target}'", installer)
            if hotkey:
                self.assertIn(f"Hotkey = '{hotkey}'", installer)
            path = ROOT / target
            self.assertTrue(path.is_file(), target)
            wrapper = path.read_text(encoding="utf-8-sig").casefold()
            self.assertIn("exit /b", wrapper, target)


if __name__ == "__main__":
    unittest.main()
