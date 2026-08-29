import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def test_parallel_first_use_returns_one_persisted_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "default.json").write_text(
                '{"assistant":{"name":"Test","default_role":"dev"},'
                '"paths":{"llama_server":"server.exe","models_dir":"models",'
                '"runtime_dir":"runtime"},'
                '"server":{"host":"127.0.0.1","port":1234},'
                '"models":{"dev":{"label":"Dev","filename":"dev.gguf",'
                '"context_size":4096,"gpu_layers":1}}}',
                encoding="utf-8",
            )
            settings = load_settings(root)
            with ThreadPoolExecutor(max_workers=8) as pool:
                values = list(pool.map(lambda _index: local_api_key(settings), range(24)))

            self.assertEqual(len(set(values)), 1)
            self.assertEqual(api_key_file(settings).read_text(encoding="utf-8").strip(), values[0])


if __name__ == "__main__":
    unittest.main()
