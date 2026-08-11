import unittest

from butler.model_evaluation import (
    check_code,
    check_exact_word,
    check_structured_russian_plan,
    check_tool,
    check_untrusted_source,
)


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
