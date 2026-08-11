import tempfile
import unittest
from pathlib import Path

from butler.config import load_settings
from butler.local_auth import api_key_file, local_api_key
from butler.model_manager import ModelManager


class LocalAuthTests(unittest.TestCase):
    def test_persistent_key_is_created_outside_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "default.json").write_text(
                '{"assistant":{"name":"Test","default_role":"dev"},'
                '"paths":{"llama_server":"server.exe","models_dir":"models","runtime_dir":"runtime"},'
                '"server":{"host":"127.0.0.1","port":1234},'
                '"models":{"dev":{"label":"Dev","filename":"dev.gguf","context_size":4096,"gpu_layers":1}}}',
                encoding="utf-8",
            )
            settings = load_settings(root)
            first = local_api_key(settings)
            self.assertEqual(first, local_api_key(settings))
            self.assertTrue(api_key_file(settings).is_file())
            command = ModelManager(settings).build_command(settings.model("dev"))
            self.assertIn("--api-key-file", command)
            self.assertIn("--cors-origins", command)
            self.assertIn("localhost", command)


if __name__ == "__main__":
    unittest.main()
