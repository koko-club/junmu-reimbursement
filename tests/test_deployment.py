from pathlib import Path
import tempfile
import unittest

from build_deployment_package import build_package


APP_DIR = Path(__file__).resolve().parents[1]


class DeploymentAssetsTest(unittest.TestCase):
    def test_deployment_builder_and_launchers_are_present(self):
        self.assertTrue((APP_DIR / "build_deployment_package.py").is_file())
        self.assertTrue((APP_DIR / "start_reimbursement_tool.sh").is_file())
        self.assertTrue((APP_DIR / "start_reimbursement_tool.bat").is_file())
        self.assertTrue((APP_DIR / "requirements.txt").is_file())
        self.assertTrue((APP_DIR / "resources" / "差旅报销单模板.xlsx").is_file())

    def test_deployment_instructions_explain_local_and_lan_modes(self):
        readme = (APP_DIR / "README_部署说明.md").read_text(encoding="utf-8")
        self.assertIn("0.0.0.0", readme)
        self.assertIn("requirements.txt", readme)
        self.assertIn("LibreOffice", readme)

    def test_source_only_archive_has_a_distinct_name(self):
        with tempfile.TemporaryDirectory() as temp_name:
            archive = build_package(APP_DIR, Path(temp_name), include_runtime=False)
            self.assertIn("源码", archive.name)

    def test_mac_launcher_selects_a_python_that_has_required_packages(self):
        launcher = (APP_DIR / "start_reimbursement_tool.sh").read_text(encoding="utf-8")
        self.assertIn("import openpyxl, PIL", launcher)
        self.assertIn("codex-primary-runtime/dependencies/python/bin/python3", launcher)


if __name__ == "__main__":
    unittest.main()
