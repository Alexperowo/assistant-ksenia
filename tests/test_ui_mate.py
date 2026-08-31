import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from butler.config import load_settings
from butler.ui_mate import UIMateProposer, UIMateProtocolError, parse_ui_mate_response


def _response(action: str, parameters: str, description: str = "Do the next step.") -> str:
    return (
        f"<action>{description}</action>\n"
        "<tool_call>\n<function=computer_use>\n"
        f"<parameter=action>{action}</parameter>\n"
        f"{parameters}\n"
        "</function>\n</tool_call>"
    )


class UIMateProtocolTests(unittest.TestCase):
    def test_parser_returns_normalized_data_and_never_source_code(self):
        proposal = parse_ui_mate_response(
            _response(
                "left_click",
                "<parameter=coordinate>[48, 280]</parameter>",
                "Open the Extensions view.",
            )
        )

        self.assertEqual(proposal.tool_name, "computer_use")
        self.assertEqual(
            dict(proposal.arguments),
            {"action": "left_click", "coordinate": (48, 280)},
        )
        self.assertNotIn("pyautogui", str(proposal.arguments))

    def test_parser_rejects_out_of_range_coordinate_and_unknown_tool(self):
        with self.assertRaisesRegex(UIMateProtocolError, "диапазон 0–999"):
            parse_ui_mate_response(
                _response("left_click", "<parameter=coordinate>[1000, 1]</parameter>")
            )
        with self.assertRaisesRegex(UIMateProtocolError, "неизвестный инструмент"):
            parse_ui_mate_response(
                _response("left_click", "<parameter=coordinate>[1, 1]</parameter>").replace(
                    "computer_use", "shell"
                )
            )

    def test_parser_rejects_multiple_actions_and_trailing_text(self):
        valid = _response("hotkey", '<parameter=keys>["ctrl", "d"]</parameter>')
        with self.assertRaisesRegex(UIMateProtocolError, "ровно один"):
            parse_ui_mate_response(valid + "\n" + valid)
        with self.assertRaisesRegex(UIMateProtocolError, "лишний текст"):
            parse_ui_mate_response(valid + "\nAlready done.")

    def test_proposer_uses_declared_service_and_request_mode(self):
        settings = load_settings()
        image_buffer = BytesIO()
        Image.new("RGB", (64, 64), "white").save(image_buffer, format="PNG")
        response = {
            "choices": [
                {
                    "message": {
                        "content": _response(
                            "hotkey",
                            '<parameter=keys>["ctrl", "d"]</parameter>',
                            "Open the bookmark dialog.",
                        )
                    }
                }
            ]
        }

        with (
            patch("butler.ui_mate.complete_chat", return_value=response) as complete,
            patch("butler.ui_mate.diagnostic_event"),
        ):
            proposal = UIMateProposer(settings).propose(
                "Bookmark this page.", image_buffer.getvalue(), mode_name="fast"
            )

        self.assertEqual(proposal.arguments["keys"], ("ctrl", "d"))
        self.assertEqual(complete.call_args.kwargs["service"].port, 18082)
        self.assertFalse(complete.call_args.kwargs["request_mode"].enable_thinking)
        sent_messages = complete.call_args.args[1]
        self.assertTrue(
            sent_messages[1]["content"][0]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
        )


if __name__ == "__main__":
    unittest.main()
