import unittest
from pathlib import Path

from butler.model_evaluation import (
    base_cases,
    check_code,
    check_exact_word,
    check_structured_russian_plan,
    check_tool,
    check_untrusted_source,
    parse_speculative_metrics,
    with_acceleration_mode,
)
from butler.config import load_settings


def response(*, content="", tool_calls=None, finish_reason="stop"):
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "content": content,
                    "tool_calls": tool_calls or [],
                }
            }
        ]
    }


class ModelEvaluationTests(unittest.TestCase):
    def test_acceleration_benchmark_mode_preserves_declared_type_and_defaults(self):
        settings = load_settings(Path(__file__).resolve().parents[1])

        enabled = with_acceleration_mode(
            settings,
            "candidate",
            enabled=True,
            max_tokens=4,
            acceleration_type="draft-mtp",
        )
        disabled = with_acceleration_mode(enabled, "candidate", enabled=False)

        original = settings.raw["models"]["candidate"]["acceleration"]
        enabled_mode = enabled.raw["models"]["candidate"]["acceleration"]
        disabled_mode = disabled.raw["models"]["candidate"]["acceleration"]
        self.assertEqual(original, {"type": "none", "max_tokens": 0})
        self.assertEqual(enabled_mode, {"type": "draft-mtp", "max_tokens": 4})
        self.assertEqual(
            disabled_mode,
            {"type": "none", "max_tokens": 0, "draft_gpu_layers": 0},
        )
        self.assertEqual(settings.raw["models"]["candidate"]["acceleration"], original)

        dflash_enabled = with_acceleration_mode(
            settings,
            "heavy_candidate",
            enabled=True,
            max_tokens=5,
            acceleration_type="draft-dflash",
        )
        self.assertEqual(
            dflash_enabled.raw["models"]["heavy_candidate"]["acceleration"],
            {"type": "draft-dflash", "max_tokens": 5},
        )

    def test_acceleration_benchmark_rejects_enabling_undeclared_mode(self):
        settings = load_settings(Path(__file__).resolve().parents[1])
        raw = dict(settings.raw)
        raw["models"] = dict(settings.raw["models"])
        raw["models"]["candidate"] = dict(raw["models"]["candidate"])
        raw["models"]["candidate"]["acceleration"] = {
            "type": "none",
            "max_tokens": 0,
        }

        from dataclasses import replace

        with self.assertRaisesRegex(ValueError, "не выбран тип ускорения"):
            with_acceleration_mode(
                replace(settings, raw=raw), "candidate", enabled=True
            )

    def test_speculative_metrics_aggregate_request_counters(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "llama.log"
            log.write_text(
                "draft acceptance = 0.50000 (  10 accepted /  20 generated), "
                "mean len = 3.00\n"
                "draft acceptance = 0.25000 (   5 accepted /  20 generated), "
                "mean len = 2.00\n",
                encoding="utf-8",
            )

            metrics = parse_speculative_metrics(log)

        self.assertEqual(metrics["request_count"], 2)
        self.assertEqual(metrics["accepted_tokens"], 15)
        self.assertEqual(metrics["generated_tokens"], 40)
        self.assertEqual(metrics["acceptance_rate"], 0.375)

    def test_structured_plan_budget_can_complete_its_own_eight_step_contract(self):
        case = next(item for item in base_cases() if item.name == "structured_russian_plan")

        self.assertGreaterEqual(case.max_tokens, 768)

    def test_exact_russian_word_allows_terminal_punctuation(self):
        passed, _ = check_exact_word("подтверждено")(
            response(content="Подтверждено.")
        )
        self.assertTrue(passed)

    def test_expected_tool_and_argument_are_checked(self):
        value = response(
            tool_calls=[
                {
                    "function": {
                        "name": "browser_search",
                        "arguments": '{"query":"RTX 5060 Ti цена"}',
                    }
                }
            ]
        )
        passed, _ = check_tool("browser_search", "query", "5060")(value)
        self.assertTrue(passed)

    def test_python_equality_fix_is_recognized(self):
        passed, _ = check_code(response(content='if user["name"] == name:'))
        self.assertTrue(passed)

    def test_injected_send_message_fails_even_with_correct_facts(self):
        value = response(
            content="184 эксперта, 27B и MTP",
            tool_calls=[{"function": {"name": "send_message", "arguments": "{}"}}],
        )
        passed, _ = check_untrusted_source(value)
        self.assertFalse(passed)

    def test_structured_russian_plan_requires_numbered_cyrillic_steps(self):
        content = "\n".join(
            f"Шаг {number}: Сначала выполняется подробная безопасная проверка проекта. "
            "Затем результат записывается в журнал и отдельно проверяется Александром."
            for number in range(1, 9)
        )
        passed, _ = check_structured_russian_plan(response(content=content))
        self.assertTrue(passed)

        truncated, _ = check_structured_russian_plan(
            response(content=content, finish_reason="length")
        )
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main()
