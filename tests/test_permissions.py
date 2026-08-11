import unittest

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


class PathOutside:
    @staticmethod
    def workspace():
        from pathlib import Path

        return Path.home() / "outside-butler-test.txt"


if __name__ == "__main__":
    unittest.main()
