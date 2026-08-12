import copy
import unittest
from dataclasses import replace
from pathlib import Path

from butler.config import load_settings
from butler.permissions import Decision, PermissionBroker


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings()
        self.broker = PermissionBroker(self.settings)

    def test_read_inside_workspace_is_allowed(self):
        result = self.broker.authorize("read_file", self.settings.root / "workspace" / "README.txt")
        self.assertEqual(result.decision, Decision.ALLOW)

    def test_assistant_configuration_is_outside_project_workspace(self):
        result = self.broker.authorize("write_file", self.settings.root / "config" / "user.json", confirmed=True)
        self.assertEqual(result.decision, Decision.DENY)

    def test_target_outside_workspace_is_denied(self):
        result = self.broker.authorize("read_file", PathOutside.workspace())
        self.assertEqual(result.decision, Decision.DENY)

    def test_confirmation_promotes_action_to_allow(self):
        target = self.settings.root / "workspace" / "example.txt"
        pending = self.broker.authorize("write_file", target)
        allowed = self.broker.authorize("write_file", target, confirmed=True)
        self.assertEqual(pending.decision, Decision.CONFIRM)
        self.assertEqual(allowed.decision, Decision.ALLOW)

    def test_user_configuration_cannot_weaken_safety_minimum(self):
        raw = copy.deepcopy(self.settings.raw)
        actions = raw["permissions"]["actions"]
        confirmation_actions = {
            "write_file",
            "run_tests",
            "run_command",
            "windows_write",
            "browser_write",
            "memory_write",
            "memory_delete",
            "delete_file",
            "install_software",
            "send_message",
        }
        for action in confirmation_actions:
            actions[action] = "allow"
        # Even a deliberately incomplete policy object must retain the product floor.
        actions.pop("install_software")
        actions["financial_action"] = "allow"
        broker = PermissionBroker(replace(self.settings, raw=raw))

        for action in confirmation_actions:
            with self.subTest(action=action):
                self.assertEqual(broker.authorize(action).decision, Decision.CONFIRM)
                self.assertEqual(
                    broker.authorize(action, confirmed=True).decision,
                    Decision.ALLOW,
                )
        self.assertEqual(
            broker.authorize("financial_action", confirmed=True).decision,
            Decision.DENY,
        )

        raw["permissions"]["actions"]["write_file"] = "deny"
        stricter = PermissionBroker(replace(self.settings, raw=raw))
        self.assertEqual(
            stricter.authorize("write_file", confirmed=True).decision,
            Decision.DENY,
        )

    def test_configured_roots_cannot_escape_declared_workspace(self):
        raw = copy.deepcopy(self.settings.raw)
        raw["permissions"]["allowed_roots"] = [str(Path.home())]
        broker = PermissionBroker(replace(self.settings, raw=raw))

        result = broker.authorize("read_file", Path.home() / "private.txt")

        self.assertEqual(result.decision, Decision.DENY)


class PathOutside:
    @staticmethod
    def workspace():
        from pathlib import Path

        return Path.home() / "outside-butler-test.txt"


if __name__ == "__main__":
    unittest.main()
