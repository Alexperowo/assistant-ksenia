import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler.cli import _start_model_role, _stop_all_model_services


class CliModelRoutingTests(unittest.TestCase):
    @patch("butler.cli.ModelResidencyCoordinator")
    def test_resident_role_starts_the_declared_pool(self, coordinator_class):
        state = SimpleNamespace(role="ui_butler", pid=10)
        coordinator_class.return_value.activate_residents.return_value = {
            "ui_butler": state
        }
        settings = Mock()
        settings.resident_model_roles.return_value = ("ui_butler", "research_fast")

        result = _start_model_role(settings, "ui_butler")

        self.assertIs(result, state)
        coordinator_class.return_value.activate_residents.assert_called_once_with()

    @patch("butler.cli.ModelManager.for_role")
    @patch("butler.cli.ModelResidencyCoordinator")
    def test_primary_role_suspends_residents_before_start(
        self, coordinator_class, for_role
    ):
        state = SimpleNamespace(role="candidate", pid=20)
        for_role.return_value.start.return_value = state
        settings = Mock()
        settings.resident_model_roles.return_value = ("ui_butler", "research_fast")

        result = _start_model_role(settings, "candidate")

        self.assertIs(result, state)
        coordinator_class.return_value.suspend_residents_for_primary.assert_called_once_with()
        for_role.assert_called_once_with(settings, "candidate")
        for_role.return_value.start.assert_called_once_with("candidate")

    @patch("butler.cli.ModelManager")
    def test_stop_covers_every_declared_service(self, manager_class):
        managers = [Mock(), Mock(), Mock()]
        for manager, stopped in zip(managers, (False, True, True), strict=True):
            manager.stop.return_value = stopped
        manager_class.side_effect = managers
        settings = Mock()
        settings.model_service_names.return_value = (
            "primary",
            "ui_fast",
            "research_fast",
        )

        stopped = _stop_all_model_services(settings)

        self.assertTrue(stopped)
        self.assertEqual(
            [call.args[1] for call in manager_class.call_args_list],
            ["research_fast", "ui_fast", "primary"],
        )


if __name__ == "__main__":
    unittest.main()
