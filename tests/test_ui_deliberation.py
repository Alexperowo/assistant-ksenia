import json
import unittest
from unittest.mock import Mock, patch

from butler.config import load_settings
from butler.ui_deliberation import (
    ActionProposal,
    ActionReview,
    PolicyActionReviewer,
    UIDeliberationError,
    UIDeliberator,
    parse_action_review,
)


class UIDeliberationTests(unittest.TestCase):
    def test_review_parser_accepts_fenced_json_but_fails_closed_on_prose(self):
        review = parse_action_review(
            "```json\n"
            + json.dumps(
                {
                    "decision": "approve",
                    "reason": "Нативное действие.",
                    "feedback": "Продолжай.",
                },
                ensure_ascii=False,
            )
            + "\n```"
        )
        self.assertTrue(review.approved)

        with self.assertRaisesRegex(UIDeliberationError, "невалидный JSON"):
            parse_action_review("Конечно, действие безопасно.")

    def test_approved_action_is_returned_without_revision(self):
        settings = load_settings()
        reviewer = Mock()
        reviewer.review.return_value = ActionReview("approve", "Верно.", "")
        source_arguments = {"x": 48, "coordinate": [48, 280]}
        proposer = Mock(
            return_value=ActionProposal(
                "Открыть расширения.", "left_click", source_arguments
            )
        )

        result = UIDeliberator(settings, reviewer=reviewer).deliberate(
            "Установи расширение.", proposer
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.revision_count, 0)
        proposer.assert_called_once_with("Установи расширение.", "")
        source_arguments["coordinate"][0] = 999
        self.assertEqual(result.proposal.arguments["coordinate"], (48, 280))
        with self.assertRaises(TypeError):
            result.proposal.arguments["x"] = 999

    def test_rejected_action_gets_one_revision_and_no_unbounded_loop(self):
        settings = load_settings()
        reviewer = Mock()
        reviewer.review.side_effect = [
            ActionReview("reject", "Терминал запрещён.", "Используй магазин приложений."),
            ActionReview("approve", "Нативный GUI.", ""),
        ]
        proposer = Mock(
            side_effect=[
                ActionProposal("Открыть терминал.", "left_click", {"x": 25, "y": 11}),
                ActionProposal("Открыть Ubuntu Software.", "left_click", {"x": 19, "y": 619}),
            ]
        )

        result = UIDeliberator(settings, reviewer=reviewer).deliberate(
            "Установи Spotify через GUI.", proposer
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.revision_count, 1)
        self.assertEqual(proposer.call_count, 2)
        self.assertEqual(
            proposer.call_args_list[1].args[1], "Используй магазин приложений."
        )

    def test_second_rejection_is_returned_without_executing_or_retrying_again(self):
        settings = load_settings()
        reviewer = Mock()
        reviewer.review.return_value = ActionReview("reject", "Неверный канал.", "Исправь.")
        proposer = Mock(
            return_value=ActionProposal("Открыть терминал.", "left_click", {"x": 1, "y": 1})
        )

        result = UIDeliberator(settings, reviewer=reviewer).deliberate("Задача", proposer)

        self.assertFalse(result.approved)
        self.assertEqual(result.revision_count, 1)
        self.assertEqual(proposer.call_count, 2)

    def test_policy_reviewer_uses_configured_resident_service_and_mode(self):
        settings = load_settings()
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "reject",
                                "reason": "Терминал запрещён.",
                                "feedback": "Используй нативный GUI.",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        with (
            patch("butler.ui_deliberation.complete_chat", return_value=response) as complete,
            patch("butler.ui_deliberation.diagnostic_event"),
        ):
            review = PolicyActionReviewer(settings).review(
                "Установи Spotify через GUI.",
                ActionProposal("Открыть терминал.", "left_click", {"x": 25, "y": 11}),
            )

        self.assertEqual(review.decision, "reject")
        self.assertEqual(complete.call_args.kwargs["service"].port, 18083)
        self.assertTrue(complete.call_args.kwargs["request_mode"].enable_thinking)
        self.assertEqual(complete.call_args.kwargs["max_tokens"], 512)


if __name__ == "__main__":
    unittest.main()
