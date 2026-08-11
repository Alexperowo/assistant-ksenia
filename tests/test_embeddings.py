import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from butler.config import load_settings
from butler.embeddings import (
    EmbeddingError,
    LlamaCppEmbeddingService,
    parse_embedding_response,
)


class EmbeddingServiceTests(unittest.TestCase):
    def test_command_is_cpu_only_loopback_and_uses_last_pooling(self):
        service = LlamaCppEmbeddingService(load_settings())
        command = service.build_command()

        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertEqual(command[command.index("--n-gpu-layers") + 1], "0")
        self.assertEqual(command[command.index("--pooling") + 1], "last")
        self.assertIn("--embedding", command)
        self.assertIn("--api-key-file", command)

    def test_openai_embedding_response_is_sorted_by_index(self):
        vectors = parse_embedding_response(
            {
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
            2,
        )
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])

    def test_malformed_embedding_response_is_rejected(self):
        with self.assertRaises(EmbeddingError):
            parse_embedding_response({"data": [{"embedding": []}]}, 1)

    def test_port_release_wait_observes_close(self):
        service = LlamaCppEmbeddingService(load_settings())
        with (
            patch.object(service, "_port_open", side_effect=[True, False]),
            patch("butler.embeddings.time.sleep") as sleep,
        ):
            self.assertTrue(service._wait_port_closed(timeout=1))
        sleep.assert_called_once()

    def test_incomplete_embedding_model_is_rejected_before_process_start(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "llama-server.exe"
            server.touch()
            settings = replace(
                load_settings(), llama_server=server, runtime_dir=root / "runtime"
            )
            service = LlamaCppEmbeddingService(settings)
            service.model_path = root / "embedding.gguf"
            service.model_path.write_bytes(b"incomplete")
            service.expected_size_bytes = 100
            with patch("butler.embeddings.subprocess.Popen") as popen:
                with self.assertRaisesRegex(EmbeddingError, "неверный размер"):
                    service.start()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
