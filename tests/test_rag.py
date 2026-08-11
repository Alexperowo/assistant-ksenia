import tempfile
import unittest
from pathlib import Path

from butler.rag import HybridRagIndex, chunk_text


class FakeEmbedder:
    model_id = "fake-russian-v1"

    VOCABULARY = ("микрофон", "голос", "цена", "товар", "ошибка", "python")

    def embed(self, texts, *, kind="document"):
        vectors = []
        for text in texts:
            normalized = str(text).casefold()
            vector = [float(normalized.count(word)) for word in self.VOCABULARY]
            if not any(vector):
                vector[-1] = 0.01
            vectors.append(vector)
        return vectors


class HybridRagTests(unittest.TestCase):
    def test_fts_query_removes_common_russian_stopwords(self):
        query = HybridRagIndex._fts_query(
            "где в программе перехватывается исключение при чтении файла"
        )
        self.assertNotIn('"где"', query)
        self.assertNotIn('"при"', query)
        self.assertIn('"исключение"', query)

    def test_health_checks_database_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            health = HybridRagIndex(Path(directory)).health()
            self.assertEqual(health["documents"], 0)
            self.assertEqual(health["chunks"], 0)
            self.assertEqual(health["integrity"], "ok")

    def test_chunks_keep_line_citations(self):
        chunks = chunk_text("первая\nвторая\nтретья\nчетвёртая", target_chars=14, overlap_lines=1)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0].start_line, 1)
        self.assertGreaterEqual(chunks[1].start_line, 2)

    def test_hybrid_search_finds_semantic_and_exact_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = HybridRagIndex(root)
            embedder = FakeEmbedder()
            index.index_text(
                "project",
                "audio.md",
                "Настройка микрофона и качественного голосового ввода.",
                modified_ns=1,
                embedder=embedder,
            )
            index.index_text(
                "project",
                "shop.md",
                "Проверка цены и наличия товара у двух продавцов.",
                modified_ns=2,
                embedder=embedder,
            )

            voice = index.search("project", "проблема голосового микрофона", embedder=embedder)
            price = index.search("project", "актуальная цена товара", embedder=embedder)

            self.assertEqual(voice[0].path, "audio.md")
            self.assertEqual(price[0].path, "shop.md")
            self.assertEqual(voice[0].start_line, 1)

    def test_workspace_index_is_incremental_and_skips_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace = base / "workspace"
            workspace.mkdir()
            (workspace / "main.py").write_text("print('голос')", encoding="utf-8")
            (workspace / ".env").write_text("PASSWORD=secret", encoding="utf-8")
            (workspace / "server.pem").write_text("PRIVATE KEY", encoding="utf-8")
            (workspace / "node_modules").mkdir()
            (workspace / "node_modules" / "ignored.js").write_text("товар", encoding="utf-8")
            index = HybridRagIndex(base / "runtime")
            embedder = FakeEmbedder()

            first = index.index_workspace(workspace, namespace="project", embedder=embedder)
            second = index.index_workspace(workspace, namespace="project", embedder=embedder)

            self.assertEqual(first.indexed_files, 1)
            self.assertEqual(second.unchanged_files, 1)
            self.assertEqual(index.search("project", "голос", embedder=embedder)[0].path, "main.py")


if __name__ == "__main__":
    unittest.main()
