import unittest
from pathlib import Path

from butler.model_evaluation import (
    base_cases,
    check_code,
    check_exact_word,
    check_structured_russian_plan,
    check_tool,
    check_untrusted_source,
    with_mtp_mode,
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
    def test_mtp_benchmark_mode_is_explicit_and_does_not_mutate_defaults(self):
        settings = load_settings(Path(__file__).resolve().parents[1])

        enabled = with_mtp_mode(settings, "candidate", enabled=True)
        disabled = with_mtp_mode(enabled, "candidate", enabled=False)

        original = settings.raw["models"]["candidate"]["acceleration"]
        enabled_mode = enabled.raw["models"]["candidate"]["acceleration"]
        disabled_mode = disabled.raw["models"]["candidate"]["acceleration"]
        self.assertEqual(original, {"type": "draft-mtp", "max_tokens": 2})
        self.assertEqual(enabled_mode, {"type": "draft-mtp", "max_tokens": 2})
        self.assertEqual(disabled_mode, {"type": "none", "max_tokens": 0})
        self.assertEqual(settings.raw["models"]["candidate"]["acceleration"], original)

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
