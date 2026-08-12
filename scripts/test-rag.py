from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from butler.config import load_settings  # noqa: E402
from butler.embeddings import LlamaCppEmbeddingService  # noqa: E402
from butler.rag import HybridRagIndex  # noqa: E402


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    settings = load_settings(ROOT)
    config = settings.raw.get("rag", {})
    if not bool(config.get("enabled", False)):
        print("ПОЗЖЕ: RAG выключен в конфигурации; живая проверка эмбеддера пропущена.")
        return 0
    model = Path(str(config.get("model_path", ""))).resolve()
    expected_size = int(config.get("expected_size_bytes", 0) or 0)
    expected_hash = str(config.get("sha256", "")).casefold()
    if not model.is_file():
        print(f"ОШИБКА: модель эмбеддингов не найдена: {model}")
        return 2
    if expected_size and model.stat().st_size != expected_size:
        print("ОШИБКА: размер модели эмбеддингов не совпадает.")
        return 2
    actual_hash = file_sha256(model)
    if expected_hash and actual_hash != expected_hash:
        print("ОШИБКА: SHA-256 модели эмбеддингов не совпадает.")
        return 2

    report_dir = settings.runtime_dir / "rag"
    report_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "started_at": datetime.now().astimezone().isoformat(),
        "model": str(model),
        "sha256": actual_hash,
        "queries": [],
    }
    tests = [
        ("как исправить ситуацию, когда Ксения не слышит фразу активации", "audio.md"),
        ("сравнить стоимость видеокарты в нескольких магазинах", "products.md"),
        ("где в программе перехватывается исключение при чтении файла", "code.py"),
        ("точная настройка wake_device", "audio.md"),
        ("как приготовить яблочный пирог", ""),
    ]
    passed = 0
    tests_root = report_dir / "tests"
    tests_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tests_root) as directory:
        workspace = Path(directory)
        (workspace / "audio.md").write_text(
            "# Голос\nКсения ожидает фразу активации и после ответа «Слушаю» открывает диктовку.\n"
            "Параметр wake_device выбирает микрофон Bluetooth. При ошибке нужно проверить уровень сигнала.",
            encoding="utf-8",
        )
        (workspace / "products.md").write_text(
            "# Исследование товаров\nДля покупки видеокарты сравнивают стоимость, наличие и доставку "
            "минимум у двух продавцов. Данные берут с прямых страниц магазинов.",
            encoding="utf-8",
        )
        (workspace / "code.py").write_text(
            "def read_text(path):\n    try:\n        return path.read_text(encoding='utf-8')\n"
            "    except OSError as exc:\n        raise RuntimeError('Файл не прочитан') from exc\n",
            encoding="utf-8",
        )
        index = HybridRagIndex(report_dir / "test-runtime")
        service = LlamaCppEmbeddingService(settings)
        try:
            summary = index.index_workspace(
                workspace, namespace="russian-live-test", embedder=service
            )
            unchanged = index.index_workspace(
                workspace, namespace="russian-live-test", embedder=service
            )
            report["first_index"] = summary.__dict__
            report["second_index"] = unchanged.__dict__
            for query, expected_path in tests:
                results = index.search(
                    "russian-live-test", query, embedder=service, limit=3
                )
                actual_path = results[0].path if results else ""
                ok = actual_path == expected_path
                passed += int(ok)
                report["queries"].append(
                    {
                        "query": query,
                        "expected": expected_path,
                        "actual": actual_path,
                        "passed": ok,
                        "results": [item.as_dict() for item in results],
                    }
                )
                print(f"{'ГОТОВО' if ok else 'ОШИБКА'}: {query} -> {actual_path}")
        finally:
            service.close()
        report["embedding_service_stopped"] = service.running_state() is None

    report["summary"] = {"passed": passed, "total": len(tests)}
    report["finished_at"] = datetime.now().astimezone().isoformat()
    target = report_dir / "latest.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Отчёт: {target}")
    return 0 if passed == len(tests) and report["embedding_service_stopped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
