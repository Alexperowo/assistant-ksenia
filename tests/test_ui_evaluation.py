import json
import tempfile
import unittest
from pathlib import Path

from butler.ui_deliberation import ActionProposal
from butler.ui_evaluation import (
    UIEvaluationError,
    evaluate_ui_proposal,
    load_ui_manifest,
)


class UIEvaluationTests(unittest.TestCase):
    def _manifest(self, root: Path, case: dict) -> Path:
        (root / "screen.png").write_bytes(b"png")
        path = root / "manifest.json"
        path.write_text(
            json.dumps({"version": 1, "cases": [case]}), encoding="utf-8"
        )
        return path

    def test_manifest_and_coordinate_region_accept_expected_click(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(
                root,
                {
                    "id": "start-button",
                    "task": "Open Start.",
                    "screenshot": "screen.png",
                    "expectations": [
                        {
                            "action": "left_click",
                            "coordinate_region": [0, 900, 100, 999],
                        }
                    ],
                },
            )
            case = load_ui_manifest(manifest)[0]

            result = evaluate_ui_proposal(
                case,
                ActionProposal(
                    "Click Start.",
                    "computer_use",
                    {"action": "left_click", "coordinate": [14, 971]},
                ),
            )

            self.assertTrue(result.passed)
            self.assertEqual(result.reasons, ())

    def test_alternatives_accept_shortcut_and_report_unmatched_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._manifest(
                root,
                {
                    "id": "bookmark",
                    "task": "Bookmark page.",
                    "screenshot": "screen.png",
                    "expectations": [
                        {"action": "hotkey", "keys": ["ctrl", "d"]},
                        {
                            "action": "left_click",
                            "coordinate_region": [900, 100, 950, 150],
                        },
                    ],
                    "forbidden_actions": ["type"],
                },
            )
            case = load_ui_manifest(manifest)[0]

            result = evaluate_ui_proposal(
                case,
                ActionProposal(
                    "Wrong.",
                    "computer_use",
                    {"action": "type", "text": "wrong"},
                ),
            )

            self.assertFalse(result.passed)
            self.assertEqual(len(result.reasons), 2)

            accepted = evaluate_ui_proposal(
                case,
                ActionProposal(
                    "Shortcut.",
                    "computer_use",
                    {"action": "hotkey", "keys": ["ctrl", "d"]},
                ),
            )
            self.assertTrue(accepted.passed)

    def test_manifest_rejects_traversal_unknown_action_and_duplicate_id(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "corpus"
            root.mkdir()
            outside = base / "outside.png"
            outside.write_bytes(b"png")
            path = root / "manifest.json"
            case = {
                "id": "same",
                "task": "Task.",
                "screenshot": "../outside.png",
                "expectations": [{"action": "shell"}],
            }
            path.write_text(
                json.dumps({"version": 1, "cases": [case, case]}), encoding="utf-8"
            )

            with self.assertRaises(UIEvaluationError):
                load_ui_manifest(path)

    def test_manifest_rejects_invalid_constraints_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                {
                    "id": "invalid",
                    "task": "Task.",
                    "screenshot": "screen.png",
                    "expectations": [
                        {"action": "hotkey", "coordinate_region": [0, 0, 10, 10]}
                    ],
                },
                {
                    "id": "typo",
                    "task": "Task.",
                    "screenshot": "screen.png",
                    "expectations": [{"action": "hotkey", "keys": ["ctrl", "d"]}],
                    "expected_action": "hotkey",
                },
                {
                    "id": "unbounded-click",
                    "task": "Task.",
                    "screenshot": "screen.png",
                    "expectations": [{"action": "left_click"}],
                },
                {
                    "id": "unchecked-type",
                    "task": "Task.",
                    "screenshot": "screen.png",
                    "expectations": [{"action": "type"}],
                },
            )
            for case in cases:
                with self.subTest(case_id=case["id"]):
                    manifest = self._manifest(root, case)
                    with self.assertRaises(UIEvaluationError):
                        load_ui_manifest(manifest)

    def test_readonly_runner_has_no_input_or_executor_dependency(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "evaluate_ui_mate_readonly.py"
        ).read_text(encoding="utf-8")

        for forbidden in (
            "windows_automation",
            "windows_bridge",
            "pyautogui",
            "ToolExecutor",
            "SendInput",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
