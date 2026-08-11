import unittest

from butler.approval import approval_explanation, approval_scope, reusable_approval


class ApprovalTests(unittest.TestCase):
    def test_related_file_changes_share_task_scope(self):
        self.assertEqual(
            approval_scope("write_workspace_file"),
            approval_scope("replace_in_workspace_file"),
        )

    def test_external_or_destructive_actions_are_not_reused(self):
        self.assertFalse(reusable_approval("delete_workspace_file"))
        self.assertFalse(reusable_approval("send_message"))
        self.assertFalse(reusable_approval("financial_action"))

    def test_explanation_is_accessible(self):
        self.assertIn("до её завершения", approval_explanation("windows_type_text"))


if __name__ == "__main__":
    unittest.main()
