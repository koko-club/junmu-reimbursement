import os
import re
from pathlib import Path
import subprocess
import unittest

APP_DIR = Path(__file__).resolve().parents[1]


def _env_values(text):
    """Read the small KEY=value subset used by the checked-in env template."""
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if not match:
            continue
        value = match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[match.group(1)] = value
    return values


def _unquote_yaml_scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _compose_top_level_value(text, key):
    match = re.search(rf"(?m)^{re.escape(key)}[ \t]*:[ \t]*(.*?)\s*$", text)
    return _unquote_yaml_scalar(match.group(1)) if match else None


def _compose_service_names(text):
    lines = text.splitlines()
    services_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "services:" and not line.startswith(" ")),
        None,
    )
    if services_index is None:
        return []
    names = []
    for line in lines[services_index + 1:]:
        if line and not line.startswith((" ", "\t")):
            break
        match = re.fullmatch(r"  ([A-Za-z0-9][A-Za-z0-9_-]*)[ \t]*:", line)
        if match:
            names.append(match.group(1))
    return names


def _compose_service_block(text, service_name):
    lines = text.splitlines()
    services_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "services:" and not line.startswith(" ")),
        None,
    )
    if services_index is None:
        return [], []
    service_pattern = re.compile(rf"^  {re.escape(service_name)}:\s*$")
    service_index = next(
        (index for index in range(services_index + 1, len(lines))
         if service_pattern.match(lines[index])),
        None,
    )
    if service_index is None:
        return [], []
    end = len(lines)
    for index in range(service_index + 1, len(lines)):
        if lines[index] and not lines[index].startswith((" ", "\t")):
            end = index
            break
    return lines[service_index:end], lines[services_index + 1:end]


def _compose_mapping_value(lines, key, indent=4):
    prefix = " " * indent
    match = next(
        (
            candidate
            for line in lines
            if line.startswith(prefix)
            for candidate in [re.match(rf"^{prefix}{re.escape(key)}[ \t]*:[ \t]*(.*?)\s*$", line)]
            if candidate is not None
        ),
        None,
    )
    return _unquote_yaml_scalar(match.group(1)) if match else None


def _compose_volume_entries(service_lines):
    """Return (source, target) pairs for short- or long-form bind mounts."""
    entries = []
    in_volumes = False
    long_entry = {}

    def flush_long_entry():
        if long_entry.get("source") and long_entry.get("target"):
            entries.append((long_entry["source"], long_entry["target"]))
        long_entry.clear()

    for line in service_lines:
        if re.match(r"^    volumes[ \t]*:", line):
            in_volumes = True
            continue
        if not in_volumes:
            continue
        if line and not line.startswith(" "):
            break
        if re.match(r"^    [A-Za-z0-9_-]+[ \t]*:", line) and not line.startswith("      "):
            flush_long_entry()
            break
        short_match = re.match(r"^\s*-\s*(.*?)\s*$", line)
        if short_match:
            flush_long_entry()
            value = _unquote_yaml_scalar(short_match.group(1))
            # A greedy source keeps the colon in ${VAR:-fallback} intact.
            match = re.match(r"^(.+):(/(?:data|backups))$", value)
            if match:
                entries.append((match.group(1), match.group(2)))
            continue
        long_match = re.match(r"^\s+(source|target)[ \t]*:[ \t]*(.*?)\s*$", line)
        if long_match:
            long_entry[long_match.group(1)] = _unquote_yaml_scalar(long_match.group(2))
    flush_long_entry()
    return entries


def _resolve_env_interpolation(value, env_values):
    def replace(match):
        key, fallback = match.groups()
        return env_values.get(key, fallback or "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-?+](.*))?\}", replace, value)


class DockerAssetsTest(unittest.TestCase):
    def test_version_and_synology_environment_paths_are_pinned(self):
        version = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "v1.0.2")
        self.assertRegex(version, r"^v\d+\.\d+\.\d+$")

        env_example = APP_DIR / ".env.example"
        self.assertTrue(env_example.is_file(), ".env.example is required")
        values = _env_values(env_example.read_text(encoding="utf-8"))
        self.assertEqual(values.get("APP_VERSION"), version)
        self.assertEqual(values.get("DATA_PATH"), "/volume1/docker/baoxiao/data")
        self.assertEqual(values.get("BACKUP_PATH"), "/volume1/docker/baoxiao/backups")
        for key in ("DATA_PATH", "BACKUP_PATH"):
            with self.subTest(path_key=key):
                value = values.get(key, "")
                self.assertTrue(value.startswith("/"), "persistent host paths must be absolute")
                self.assertTrue(value.startswith("/volume1/docker/baoxiao/"))
                self.assertNotRegex(value, r"(?:^|/)\.\.?(?:/|$)")
        data_path = values.get("DATA_PATH", "").rstrip("/")
        backup_path = values.get("BACKUP_PATH", "").rstrip("/")
        self.assertNotEqual(data_path, backup_path)
        self.assertFalse(data_path.startswith(backup_path + "/"))
        self.assertFalse(backup_path.startswith(data_path + "/"))
        self.assertNotIn("/volume1/docker/baoxiao", (data_path, backup_path))

    def test_image_installs_office_chinese_fonts_and_runs_as_nonroot(self):
        dockerfile = APP_DIR / "Dockerfile"
        self.assertTrue(dockerfile.is_file(), "Dockerfile is required")
        text = dockerfile.read_text()
        for required in ("python:3.12-slim-bookworm", "libreoffice-calc", "libreoffice-core", "fontconfig",
                         "fonts-noto-cjk", "poppler-utils", "--no-install-recommends", "requirements-test.txt", "USER app",
                         "HOME=/tmp/app-home", "EXPOSE 8800", 'CMD ["python", "app.py"]'):
            self.assertIn(required, text)

    def test_dockerfile_declares_version_arg_and_oci_label(self):
        dockerfile = APP_DIR / "Dockerfile"
        self.assertTrue(dockerfile.is_file(), "Dockerfile is required")
        text = dockerfile.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^ARG[ \t]+APP_VERSION(?:[ \t]*=.*)?[ \t]*$")
        self.assertRegex(
            text,
            r"(?m)^\s*LABEL\b[^\n]*org\.opencontainers\.image\.version[ \t]*=[ \t]*(?:\"?\$\{?APP_VERSION\}?\"?)",
        )

    def test_compose_pins_platform_and_persistent_mounts(self):
        compose = APP_DIR / "compose.yaml"
        self.assertTrue(compose.is_file(), "compose.yaml is required")
        text = compose.read_text()
        self.assertEqual(_compose_top_level_value(text, "name"), "junmu-reimbursement")
        self.assertNotRegex(text, r"(?m)^name[ \t]*:[ \t]*(?:reimbursement|reimbursement-system)")
        self.assertEqual(_compose_service_names(text), ["junmu-reimbursement"])
        service_lines, service_scope = _compose_service_block(text, "junmu-reimbursement")
        self.assertTrue(service_lines, "services must contain junmu-reimbursement")

        service_text = "\n".join(service_lines)
        image = _compose_mapping_value(service_lines, "image")
        container_name = _compose_mapping_value(service_lines, "container_name")
        self.assertRegex(image or "", r"^junmu-reimbursement:\$\{APP_VERSION(?:[^}]*)\}$")
        self.assertRegex(container_name or "", r"^junmu-reimbursement-\$\{APP_VERSION(?:[^}]*)\}$")
        env_values = _env_values((APP_DIR / ".env.example").read_text(encoding="utf-8"))
        self.assertEqual(_resolve_env_interpolation(image or "", env_values), "junmu-reimbursement:v1.0.2")
        self.assertEqual(
            _resolve_env_interpolation(container_name or "", env_values),
            "junmu-reimbursement-v1.0.2",
        )
        for required in (
            "linux/amd64", "${PUID:-1000}:${PGID:-1000}", "APP_HOST: 0.0.0.0",
            "APP_PORT: 8800", "APP_DATA_DIR: /data", "APP_SOFFICE_PATH: /usr/bin/soffice",
            "APP_COOKIE_SECURE", "Asia/Shanghai", '"8800:8800"', "unless-stopped",
            "urllib.request", "/healthz", "healthcheck:",
        ):
            self.assertIn(required, service_text)
        volumes = _compose_volume_entries(service_lines)
        mounts = {target: source for source, target in volumes}
        self.assertIn("/data", mounts)
        self.assertIn("/backups", mounts)
        expected_paths = {
            "/data": ("DATA_PATH", "/volume1/docker/baoxiao/data"),
            "/backups": ("BACKUP_PATH", "/volume1/docker/baoxiao/backups"),
        }
        for target, (key, expected_path) in expected_paths.items():
            with self.subTest(mount=target):
                source = mounts[target]
                self.assertRegex(source, rf"^\$\{{{key}(?::[-?+][^}}]*)?\}}$")
                resolved = _resolve_env_interpolation(source, env_values)
                self.assertEqual(resolved, expected_path)
                self.assertTrue(resolved.startswith("/volume1/docker/baoxiao/"))
                self.assertNotEqual(resolved, "/volume1/docker/baoxiao")
                self.assertNotRegex(source, r"(?:^|:)\./")
        all_sources = [source for source, _ in volumes]
        self.assertFalse(any(source in ("/volume1/docker/baoxiao", "${DATA_PATH:-/volume1/docker/baoxiao}",
                                         "${BACKUP_PATH:-/volume1/docker/baoxiao}")
                                 for source in all_sources))
        self.assertNotRegex(service_text, r"(?m)^\s*-\s*[\"']?/volume1/docker/baoxiao[\"']*:/")
        self.assertNotIn("reimbursement-system:latest", text)
        self.assertNotRegex(text, r"(?i)\breimbursement-system\b")
        self.assertNotRegex(text, r"(?i):latest\b")
        self.assertNotIn("/api/health", text)

    def test_build_context_excludes_private_runtime_data(self):
        ignore = APP_DIR / ".dockerignore"
        self.assertTrue(ignore.is_file(), ".dockerignore is required")
        patterns = ignore.read_text().splitlines()
        for required in (
            "data", "**/data", "backups", "**/backups", "generated", "**/generated",
            ".env", ".env.*", "**/.env", "**/.env.*", ".git", ".venv", ".worktrees",
            "docs", "**/docs",
            "dist", "runtime", "vendor", "*.db", "**/*.db", "*.db-*", "**/*.db-*",
            "*.sqlite*", "**/*.sqlite*", "app-secret", "**/app-secret", "*.pdf", "**/*.pdf",
            "*.log", "**/*.log", ".DS_Store", "**/.DS_Store", "._*", "**/._*",
        ):
            self.assertIn(required, patterns)
        reinclusions = [line.strip() for line in patterns if line.strip().startswith("!")]
        self.assertEqual(
            reinclusions,
            ["!.env.example"],
            "only the checked-in root environment template may be re-included",
        )
        self.assertGreater(patterns.index("!.env.example"), patterns.index(".env.*"))

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
