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
        self.assertFalse(reusable_approval("browser_interact"))
        self.assertFalse(reusable_approval("windows_type_text"))
        self.assertFalse(reusable_approval("windows_click_pointer"))

    def test_explanation_is_accessible(self):
        self.assertIn("только к одному действию", approval_explanation("windows_type_text"))

    def test_safe_navigation_shares_task_scope_and_allows_reuse(self):
        self.assertTrue(reusable_approval("windows_activate_window"))
        self.assertTrue(reusable_approval("windows_move_pointer"))
        self.assertTrue(reusable_approval("windows_scroll_pointer"))
        self.assertEqual(approval_scope("windows_activate_window"), "windows_control")
        self.assertIn("управления Windows", approval_explanation("windows_activate_window"))


if __name__ == "__main__":
    unittest.main()
