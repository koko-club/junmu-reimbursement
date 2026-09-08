import os
from pathlib import Path
import subprocess
import unittest

APP_DIR = Path(__file__).resolve().parents[1]


class DockerAssetsTest(unittest.TestCase):
    def test_image_installs_office_chinese_fonts_and_runs_as_nonroot(self):
        dockerfile = APP_DIR / "Dockerfile"
        self.assertTrue(dockerfile.is_file(), "Dockerfile is required")
        text = dockerfile.read_text()
        for required in ("python:3.12-slim-bookworm", "libreoffice-calc", "libreoffice-core", "fontconfig",
                         "fonts-noto-cjk", "poppler-utils", "--no-install-recommends", "requirements-test.txt", "USER app",
                         "HOME=/tmp/app-home", "EXPOSE 8800", 'CMD ["python", "app.py"]'):
            self.assertIn(required, text)

    def test_compose_pins_platform_and_persistent_mounts(self):
        compose = APP_DIR / "compose.yaml"
        self.assertTrue(compose.is_file(), "compose.yaml is required")
        text = compose.read_text()
        for required in ("reimbursement:", "reimbursement-system:latest", "linux/amd64",
                         "${PUID:-1000}:${PGID:-1000}", "APP_HOST: 0.0.0.0", "APP_PORT: 8800",
                         "APP_DATA_DIR: /data", "APP_SOFFICE_PATH: /usr/bin/soffice", "APP_COOKIE_SECURE",
                         "Asia/Shanghai", "8800:8800", "${DATA_PATH:-./data}:/data",
                         "${BACKUP_PATH:-./backups}:/backups", "unless-stopped", "urllib.request",
                         "/api/health", "healthcheck:"):
            self.assertIn(required, text)

    def test_build_context_excludes_private_runtime_data(self):
        ignore = APP_DIR / ".dockerignore"
        self.assertTrue(ignore.is_file(), ".dockerignore is required")
        patterns = ignore.read_text().splitlines()
        for required in ("data", "backups", "generated", ".env", ".git", ".venv", "dist", "runtime", "vendor"):
            self.assertIn(required, patterns)

    def test_required_container_dependencies_fail_instead_of_skipping(self):
        if os.environ.get("REQUIRE_PDF_TESTS") != "1":
            self.skipTest("set REQUIRE_PDF_TESTS=1 inside the built image")
        import pypdf
        self.assertTrue(pypdf.__version__)
        self.assertEqual(os.uname().machine, "x86_64")
        self.assertNotEqual(os.getuid(), 0)
        office = subprocess.run(["/usr/bin/soffice", "--version"], capture_output=True, text=True, check=True)
        self.assertIn("LibreOffice", office.stdout)
        fonts = subprocess.run(["fc-match", "Noto Sans CJK SC"], capture_output=True, text=True, check=True)
        self.assertIn("Noto", fonts.stdout)
        self.assertIn("CJK", fonts.stdout)


if __name__ == "__main__":
    unittest.main()
