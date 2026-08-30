import argparse
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_context_benchmark",
    ROOT / "scripts" / "runtime_context_benchmark.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimeContextBenchmarkTests(unittest.TestCase):
    def test_context_parser_accepts_the_required_sweep(self):
        self.assertEqual(
            MODULE.parse_contexts("16384,32768,49152,98304"),
            (16_384, 32_768, 49_152, 98_304),
        )

    def test_context_parser_rejects_duplicates_and_out_of_range_values(self):
        for value in ("16384,16384", "511", "1048577", ""):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                MODULE.parse_contexts(value)

    def test_context_override_does_not_mutate_base_settings(self):
        from butler.config import load_settings

        base = load_settings(ROOT)
        changed = MODULE.settings_with_context(base, "generalist", 16_384)

        self.assertEqual(base.model("generalist").context_size, 98_304)
        self.assertEqual(changed.model("generalist").context_size, 16_384)

    def test_usage_metrics_exposes_only_numeric_cache_accounting(self):
        value = MODULE._usage_metrics(
            {
                "prompt_tokens": "120",
                "completion_tokens": 8,
                "prompt_tokens_details": {"cached_tokens": 96, "text": "secret"},
            }
        )

        self.assertEqual(
            value,
            {"prompt_tokens": 120, "completion_tokens": 8, "cached_tokens": 96},
        )


if __name__ == "__main__":
    unittest.main()
