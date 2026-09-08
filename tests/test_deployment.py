import os
from pathlib import Path
import subprocess
import tempfile
import unittest

APP_DIR = Path(__file__).resolve().parents[1]


class DeploymentAssetsTest(unittest.TestCase):
    def test_supported_launchers_and_retired_packager(self):
        self.assertTrue((APP_DIR / "start_reimbursement_tool.sh").is_file())
        self.assertFalse((APP_DIR / "build_deployment_package.py").exists())
        self.assertFalse((APP_DIR / "start_reimbursement_tool.bat").exists())
        self.assertTrue((APP_DIR / "requirements.txt").is_file())
        self.assertTrue((APP_DIR / "resources" / "差旅报销单模板.xlsx").is_file())

    def test_deployment_instructions_explain_local_and_lan_modes(self):
        readme = (APP_DIR / "README_部署说明.md").read_text(encoding="utf-8")
        self.assertIn("0.0.0.0", readme)
        self.assertIn("requirements.txt", readme)
        self.assertIn("LibreOffice", readme)

    def test_mac_launcher_selects_a_python_that_has_required_packages(self):
        launcher = (APP_DIR / "start_reimbursement_tool.sh").read_text(encoding="utf-8")
        self.assertIn("import openpyxl, PIL", launcher)
        self.assertIn(".venv/bin/python", launcher)
        self.assertIn("(3, 12)", launcher)
        self.assertNotIn("codex-primary-runtime", launcher)
        self.assertNotIn("PYTHONHOME", launcher)

    def test_readme_covers_offline_import_permissions_and_restore(self):
        readme = (APP_DIR / "README_部署说明.md").read_text()
        for required in ("Container Manager", "linux/amd64", "8800",
                         "/volume1/docker/reimbursement/data", "PUID", "PGID", "chown", "局域网",
                         "--restore", "回滚", "防火墙", "purge_claim"):
            self.assertIn(required, readme)

    def test_shell_scripts_have_valid_syntax(self):
        paths = [APP_DIR / "start_reimbursement_tool.sh", APP_DIR / "start_reimbursement_tool.command"]
        paths += [APP_DIR / "scripts" / name for name in (
            "backup.sh", "build-docker.sh", "export-docker.sh", "package-synology.sh")]
        for path in paths:
            with self.subTest(script=path.name):
                self.assertTrue(path.is_file(), str(path))
                result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("set -eu", path.read_text())

    def test_backup_restarts_and_checks_health_when_backup_fails(self):
        script = APP_DIR / "scripts" / "backup.sh"
        self.assertTrue(script.is_file(), "backup script is required")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BACKUP_TEST_LOG\"\n"
                              "case \"$*\" in *'run --rm'*) exit 7;; esac\n")
            docker.chmod(0o755)
            log = root / "calls"
            environment = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "BACKUP_TEST_LOG": str(log)}
            result = subprocess.run(["sh", str(script)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, result.stderr)
            calls = log.read_text().splitlines()
            self.assertIn("stop reimbursement", calls[0])
            self.assertIn("python backup.py --data-dir /data --backup-dir /backups", calls[1])
            self.assertIn("up -d --wait", calls[2])


if __name__ == "__main__":
    unittest.main()
