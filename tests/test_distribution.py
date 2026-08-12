from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_release  # noqa: E402


class DistributionTests(unittest.TestCase):
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
        self.assertIn(".key", package["forbidden_extensions"])
        self.assertFalse(manifest["models"]["bundled"])
        defaults = json.loads(
            (ROOT / "config" / "default.json").read_text(encoding="utf-8")
        )
        serialized = json.dumps(defaults, ensure_ascii=False)
        self.assertNotRegex(serialized, r'"[A-Za-z]:[\\/]')
        self.assertEqual(defaults["paths"]["model_search_dirs"], [])

    def test_forbidden_path_guard_handles_nested_and_case_variants(self):
        rules = {
            "excluded_files": ["config/user.json"],
            "excluded_path_parts": ["runtime", "tools/llama.cpp"],
            "forbidden_extensions": [".gguf", ".key"],
        }
        self.assertTrue(validate_release._path_is_forbidden("CONFIG/user.json", rules))
        self.assertTrue(validate_release._path_is_forbidden("runtime/logs/a.json", rules))
        self.assertTrue(validate_release._path_is_forbidden("tools/llama.cpp/x.dll", rules))
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
        self.assertIn("runtime.lock.txt", installer)
        self.assertIn("torch-cu128.lock.txt", installer)
        self.assertIn("setuptools==78.1.1", runtime_lock)
        self.assertNotIn("setuptools==78.1.0", runtime_lock)
        self.assertIn("[string]$ModelStorageRoot", installer)
        self.assertIn("pip==$", installer)
        self.assertIn("chrome-win64", installer)
        self.assertIn("playwright install --no-shell chromium", installer)
        self.assertNotIn("sync_playwright", installer)
        self.assertNotIn("pip install --upgrade pip", installer)
        self.assertNotIn("snapshot_download(\n    repo_id=os.environ[\"KSENIA_LLM", installer)

    def test_update_is_locked_staged_audited_and_reversible(self):
        updater = (SCRIPTS / "update.ps1").read_text(encoding="utf-8-sig")
        engine = (SCRIPTS / "install-llama.ps1").read_text(encoding="utf-8-sig")
        rollback = (SCRIPTS / "rollback-update.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("[switch]$CheckOnly", updater)
        self.assertIn("engine.lock.json", (SCRIPTS / "maintenance.py").read_text(encoding="utf-8"))
        self.assertIn("rollback-update.ps1", updater)
        self.assertIn("check.ps1", updater)
        self.assertIn(".llama-stage-", engine)
        self.assertIn("Get-FileHash -Algorithm SHA256", engine)
        self.assertIn("rollback-displaced-engine-", rollback)
        self.assertNotIn("github.com/ggml-org/llama.cpp/releases/latest", updater + engine)

    def test_public_cmd_entrypoints_propagate_failures(self):
        manifest = json.loads(
            (ROOT / "config" / "release-manifest.json").read_text(encoding="utf-8")
        )
        for relative in manifest["entry_points"].values():
            text = (ROOT / relative).read_text(encoding="utf-8-sig").casefold()
            self.assertIn("errorlevel 1", text, relative)
            self.assertIn("exit /b 1", text, relative)


if __name__ == "__main__":
    unittest.main()
