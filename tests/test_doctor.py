import unittest

from butler.cli import build_parser
from butler.doctor import model_check_required


class DoctorInstallationModeTests(unittest.TestCase):
    def test_clean_install_marks_external_models_optional_only_for_that_gate(self):
        self.assertTrue(model_check_required(True, installation_mode=False))
        self.assertFalse(model_check_required(True, installation_mode=True))
        self.assertFalse(model_check_required(False, installation_mode=False))

    def test_cli_exposes_explicit_installation_mode(self):
        args = build_parser().parse_args(["doctor", "--installation-mode"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.installation_mode)
