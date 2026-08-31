from __future__ import annotations

import argparse
import io
import random
import sys
import unittest
import warnings
from pathlib import Path


MINIMUM_TEST_COUNT = 393
ORDER_AUDIT_SEEDS = (17, 73, 211)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Надёжный запуск автотестов Ксении")
    parser.add_argument(
        "--order-audit",
        action="store_true",
        help="после основного запуска повторить тесты в трёх перемешанных порядках",
    )
    return parser.parse_args()


def _flatten(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    result: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            result.extend(_flatten(item))
        else:
            result.append(item)
    return result


def _discover(root: Path) -> unittest.TestSuite:
    return unittest.defaultTestLoader.discover(str(root / "tests"))


def _validate_inventory(suite: unittest.TestSuite) -> tuple[bool, str]:
    tests = _flatten(suite)
    identifiers = [test.id() for test in tests]
    if len(tests) < MINIMUM_TEST_COUNT:
        return (
            False,
            f"Найдено только {len(tests)} тестов; ожидается не меньше {MINIMUM_TEST_COUNT}.",
        )
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        return False, "Повторяющиеся тесты: " + ", ".join(duplicates)
    return True, f"Обнаружено тестов: {len(tests)}; идентификаторы уникальны."


def _run_shuffled(root: Path, seed: int) -> tuple[bool, str]:
    tests = _flatten(_discover(root))
    random.Random(seed).shuffle(tests)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=0).run(
        unittest.TestSuite(tests)
    )
    summary = (
        f"seed={seed}: {result.testsRun} тестов, "
        f"ошибок={len(result.errors)}, сбоев={len(result.failures)}, "
        f"пропущено={len(result.skipped)}"
    )
    if not result.wasSuccessful():
        summary += "\n" + output.getvalue()
    return result.wasSuccessful(), summary


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        suite = _discover(root)
        inventory_ok, inventory_message = _validate_inventory(suite)
        print(f"[ИНВЕНТАРЬ] {inventory_message}")
        if not inventory_ok:
            return 2

        result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
        if not result.wasSuccessful():
            return 1
        if result.skipped:
            print(f"[ОШИБКА] Пропущено тестов: {len(result.skipped)}")
            return 3

        if args.order_audit:
            for seed in ORDER_AUDIT_SEEDS:
                ok, summary = _run_shuffled(root, seed)
                print(f"[ПОРЯДОК] {summary}")
                if not ok:
                    return 4

    print("[ГОТОВО] Предупреждений Python нет; состав и порядок тестов проверены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
