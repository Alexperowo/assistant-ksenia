from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from butler.model_assets import (
    ModelAsset,
    ModelAssetError,
    download_model_asset,
    model_assets_from_config,
    verify_model_asset,
)


def asset_mapping(payload: bytes, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "filename": "candidate.gguf",
        "source_repo": "owner/repository",
        "source_revision": "a" * 40,
        "source_filename": "candidate.gguf",
        "expected_size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    value.update(overrides)
    return value


class ModelAssetTests(unittest.TestCase):
    def test_full_verification_checks_size_and_sha256(self):
        payload = b"small deterministic GGUF fixture"
        asset = ModelAsset.from_mapping("candidate", asset_mapping(payload))
        with tempfile.TemporaryDirectory() as directory:
            models_dir = Path(directory)
            (models_dir / asset.local_filename).write_bytes(payload)

            report = verify_model_asset(asset, models_dir, verify_hash=True)

        self.assertEqual(report["profile"], "candidate")
        self.assertEqual(report["size_bytes"], len(payload))
        self.assertTrue(report["sha256_matches"])

    def test_verification_fails_closed_on_wrong_hash(self):
        payload = b"expected"
        asset = ModelAsset.from_mapping("candidate", asset_mapping(payload))
        with tempfile.TemporaryDirectory() as directory:
            models_dir = Path(directory)
            (models_dir / asset.local_filename).write_bytes(b"tampered")

            with self.assertRaises(ModelAssetError):
                verify_model_asset(asset, models_dir, verify_hash=True)

    def test_verification_rejects_symbolic_link_before_resolution(self):
        payload = b"expected"
        asset = ModelAsset.from_mapping("candidate", asset_mapping(payload))
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / asset.local_filename
            with patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaisesRegex(ModelAssetError, "Символическая ссылка"):
                    verify_model_asset(asset, Path(directory), verify_hash=True)

    def test_asset_rejects_floating_revision_and_nested_filename(self):
        payload = b"fixture"
        with self.assertRaises(ModelAssetError):
            ModelAsset.from_mapping(
                "candidate", asset_mapping(payload, source_revision="main")
            )
        with self.assertRaises(ModelAssetError):
            ModelAsset.from_mapping(
                "candidate", asset_mapping(payload, filename="../candidate.gguf")
            )

    def test_profile_catalog_enumerates_every_declared_artifact(self):
        payload = b"fixture"
        model = asset_mapping(
            payload, filename="main.gguf", source_filename="main.gguf"
        )
        draft = asset_mapping(
            payload, filename="draft.gguf", source_filename="draft.gguf"
        )
        projector = asset_mapping(
            payload,
            filename="projector.gguf",
            source_filename="projector.gguf",
        )

        assets = model_assets_from_config(
            {
                "profile": {
                    "artifacts": {
                        "model": model,
                        "draft": draft,
                        "projector": projector,
                    }
                }
            },
            "profile",
        )

        self.assertEqual(
            tuple((asset.asset, asset.local_filename) for asset in assets),
            (
                ("model", "main.gguf"),
                ("draft", "draft.gguf"),
                ("projector", "projector.gguf"),
            ),
        )

    @patch("butler.model_assets.hf_hub_download")
    def test_download_uses_pinned_revision_without_ambient_token(self, hub_download):
        payload = b"downloaded fixture"
        asset = ModelAsset.from_mapping("candidate", asset_mapping(payload))
        with tempfile.TemporaryDirectory() as directory:
            models_dir = Path(directory)

            def fake_download(**kwargs):
                target = Path(kwargs["local_dir"]) / asset.source_filename
                target.write_bytes(payload)
                return str(target)

            hub_download.side_effect = fake_download
            report = download_model_asset(asset, models_dir)

        self.assertTrue(report["sha256_matches"])
        hub_download.assert_called_once_with(
            repo_id=asset.repo,
            filename=asset.source_filename,
            revision=asset.revision,
            local_dir=str(models_dir.resolve()),
            token=False,
        )

    @patch("butler.model_assets.hf_hub_download")
    def test_download_error_does_not_expose_remote_details(self, hub_download):
        payload = b"downloaded fixture"
        asset = ModelAsset.from_mapping("candidate", asset_mapping(payload))
        hub_download.side_effect = RuntimeError(
            "https://huggingface.invalid/model?token=very-secret"
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ModelAssetError) as raised:
                download_model_asset(asset, Path(directory))

        message = str(raised.exception)
        self.assertIn("RuntimeError", message)
        self.assertNotIn("very-secret", message)
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
