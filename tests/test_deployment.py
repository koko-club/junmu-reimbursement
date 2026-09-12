import gzip
import os
import re
import shlex
import shutil
import tarfile
from pathlib import Path
import subprocess
import tempfile
import unittest

APP_DIR = Path(__file__).resolve().parents[1]


def _make_release_fixture(root, *, version="v1.0.2", env_version="v1.0.2"):
    for directory in ("scripts", "resources", "templates", "static", "tests"):
        (root / directory).mkdir(parents=True, exist_ok=True)

    files = (
        "app.py",
        "backup.py",
        "config.py",
        "database.py",
        "generator.py",
        "office.py",
        "reimbursements.py",
        "security.py",
        "sessions.py",
        "users.py",
        "validation.py",
        "web.py",
        "config.json",
        "requirements.txt",
        "requirements-test.txt",
        "Dockerfile",
        ".dockerignore",
        "start_reimbursement_tool.sh",
        "start_reimbursement_tool.command",
        "compose.yaml",
        "CHANGELOG.md",
        "README_部署说明.md",
        "scripts/backup.sh",
        "scripts/build-docker.sh",
        "scripts/export-docker.sh",
        "scripts/package-synology.sh",
        "scripts/release-vars.sh",
    )
    for relative in files:
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(APP_DIR / relative, destination)

    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    env_text = (APP_DIR / ".env.example").read_text(encoding="utf-8")
    env_text = re.sub(r"(?m)^APP_VERSION=.*$", f"APP_VERSION={env_version}", env_text)
    (root / ".env.example").write_text(env_text, encoding="utf-8")


def _archive_relative_names(archive):
    with tarfile.open(archive, "r:gz") as handle:
        return {
            member.name.split("/", 1)[1]
            for member in handle.getmembers()
            if "/" in member.name
        }


def _archive_text(archive, relative_name):
    with tarfile.open(archive, "r:gz") as handle:
        member = next(
            member for member in handle.getmembers()
            if member.name.endswith(f"/{relative_name}")
        )
        extracted = handle.extractfile(member)
        if extracted is None:
            raise AssertionError(f"archive member is not a file: {member.name}")
        return extracted.read().decode("utf-8")


def _logical_shell_lines(text):
    """Join POSIX shell continuation lines without trying to execute them."""
    logical = []
    pending = ""
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if pending:
            line = pending + line.lstrip()
        if line.endswith("\\"):
            pending = line[:-1] + " "
        else:
            logical.append(line)
            pending = ""
    if pending:
        logical.append(pending)
    return logical


def _shell_words(line):
    try:
        return shlex.split(line, comments=True, posix=True)
    except ValueError:
        return []


def _shell_commands(text, command_name):
    """Find tokenized shell commands, tolerating assignments and `if` prefixes."""
    commands = []
    for line in _logical_shell_lines(text):
        words = _shell_words(line)
        for index, word in enumerate(words):
            if word == command_name or word.rsplit("/", 1)[-1] == command_name:
                commands.append(words[index:])
                break
    return commands


def _docker_commands(text):
    commands = []
    for line in _logical_shell_lines(text):
        words = _shell_words(line)
        for index, word in enumerate(words):
            if word == "docker":
                commands.append(words[index:])
                break
    return commands


def _copy_sources(text):
    """Extract copy/install source operands without depending on one cp spelling."""
    transfers = []
    for line in _logical_shell_lines(text):
        words = _shell_words(line)
        for index, word in enumerate(words):
            command = word.rsplit("/", 1)[-1]
            if command not in ("cp", "install", "rsync"):
                continue
            args = words[index + 1:]
            non_options = []
            target_option = False
            skip_option_value = False
            for token in args:
                if skip_option_value:
                    skip_option_value = False
                    continue
                if token in ("-t", "--target", "--target-directory", "-C", "--directory"):
                    target_option = True
                    skip_option_value = True
                    continue
                if token == "--":
                    continue
                if token.startswith("-"):
                    continue
                non_options.append(token)
            if target_option:
                sources = non_options
            else:
                sources = non_options[:-1] if len(non_options) >= 2 else []
            transfers.append((line, sources))
            break
    return transfers


def _archive_sources(text):
    """Extract tar input operands while ignoring archive/output and -C values."""
    archives = []
    for line in _logical_shell_lines(text):
        words = _shell_words(line)
        for index, word in enumerate(words):
            if word.rsplit("/", 1)[-1] != "tar":
                continue
            args = words[index + 1:]
            sources = []
            position = 0
            while position < len(args):
                token = args[position]
                if token == "--":
                    sources.extend(args[position + 1:])
                    break
                if token in ("-C", "--directory", "-f", "--file", "-T", "--files-from"):
                    position += 2
                    continue
                if token.startswith("--directory=") or token.startswith("--file=") or token.startswith("--files-from="):
                    position += 1
                    continue
                if token.startswith("-"):
                    # `-czf` has a following archive filename; the other short flags do not.
                    position += 2 if "f" in token[1:] else 1
                    continue
                sources.append(token)
                position += 1
            archives.append((line, sources))
            break
    return archives


def _forbidden_package_source(token):
    """Return a reason when a package transfer operand names runtime data."""
    normalized = token.strip().strip("\"'")
    lowered = normalized.lower()
    if normalized in (".", "./", "/", "$project_dir", "${project_dir}", "$pwd", "${pwd}"):
        return "repository root"
    if re.fullmatch(r"(?:\$\{?project_dir\}?|\$\{?pwd\}?)/\.?", normalized, re.IGNORECASE):
        return "repository root"
    if re.search(r"(?:^|/)(?:data|backups)(?:/|$)", lowered):
        return "runtime data directory"
    if re.search(r"(?:^|/)\.env(?:\.[^/]*)?(?:/|$)", lowered):
        return "environment secret"
    if re.search(r"(?:^|/)app-secret(?:/|$)", lowered):
        return "application secret"
    if re.search(r"\.(?:db(?:[-_.][^/]*)?|sqlite(?:[-_.][^/]*)?|sqlite\d*)(?:/|$)", lowered):
        return "database or SQLite sidecar"
    return None


def _variable_token(name):
    return rf"\$(?:\{{{re.escape(name)}\}}|{re.escape(name)})"


def _has_variable(token, name):
    return re.search(_variable_token(name), token) is not None


def _assert_versioned_archive(testcase, text, suffix):
    collapsed = "\n".join(_logical_shell_lines(text))
    repo = rf"(?:junmu-reimbursement|{_variable_token('IMAGE_REPO')})"
    version = _variable_token("VERSION")
    testcase.assertRegex(
        collapsed,
        rf"{repo}-{version}-{re.escape(suffix)}\.tar\.gz",
        f"archive must be named <repo>-<VERSION>-{suffix}.tar.gz",
    )


class DeploymentAssetsTest(unittest.TestCase):
    def test_supported_launchers_and_retired_packager(self):
        self.assertTrue((APP_DIR / "start_reimbursement_tool.sh").is_file())
        self.assertFalse((APP_DIR / "build_deployment_package.py").exists())
        self.assertFalse((APP_DIR / "start_reimbursement_tool.bat").exists())
        self.assertTrue((APP_DIR / "requirements.txt").is_file())
        self.assertTrue((APP_DIR / "resources" / "差旅报销单模板.xlsx").is_file())

    def test_source_package_includes_runtime_version_source(self):
        script = (APP_DIR / "scripts" / "package-synology.sh").read_text(encoding="utf-8")
        transfers = _copy_sources(script)
        self.assertTrue(transfers, "source package must have explicit transfer commands")
        source_tokens = [token for _, sources in transfers for token in sources]
        self.assertTrue(
            any(Path(token.strip("\"'")).name == "VERSION" for token in source_tokens),
            "source package must retain VERSION",
        )
        self.assertTrue(
            any("release-vars.sh" in token for token in source_tokens),
            "source package must retain the shared release variables",
        )
        for line, sources in transfers:
            for token in sources:
                with self.subTest(command=line, source=token):
                    self.assertIsNone(_forbidden_package_source(token))
        for line, sources in _archive_sources(script):
            for token in sources:
                with self.subTest(archive_command=line, source=token):
                    self.assertIsNone(_forbidden_package_source(token))
        self.assertNotRegex(script, r"(?:\$\{?PROJECT_DIR\}?|\$\{?PWD\}?)/\.(?:[\"']|\s|$)")
        self.assertNotRegex(script, r"(?m)\btar\b[^\n]*(?:^|\s)(?:/|\.)(?:\s|$)")

    def test_default_synology_package_contains_version_for_release_scripts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_release_fixture(root)
            image_archive = root / "dist" / "junmu-reimbursement-v1.0.2-linux-amd64.tar.gz"
            image_archive.parent.mkdir()
            with gzip.open(image_archive, "wb") as handle:
                handle.write(b"docker-image-placeholder")

            result = subprocess.run(
                ["sh", str(root / "scripts" / "package-synology.sh")],
                cwd=root,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            archive = root / "dist" / "junmu-reimbursement-v1.0.2-synology.tar.gz"
            names = _archive_relative_names(archive)
            self.assertIn("VERSION", names)
            self.assertIn("CHANGELOG.md", names)
            self.assertEqual(_archive_text(archive, "VERSION"), "v1.0.2\n")

    def test_source_package_rejects_symlinked_top_level_inputs(self):
        cases = (
            ("database.py", ".env.example"),
            (".env.example", "private.env"),
        )
        for relative, target in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _make_release_fixture(root)
                private = root / "private.env"
                private.write_text("APP_VERSION=v1.0.2\nPRIVATE_TOKEN=must-not-package\n", encoding="utf-8")
                path = root / relative
                path.unlink()
                path.symlink_to(root / target)

                result = subprocess.run(
                    ["sh", str(root / "scripts" / "package-synology.sh"), "--source"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0, "packaging must fail closed on top-level symlinks")
                self.assertIn("普通文件", result.stderr)
                archive = root / "dist" / "junmu-reimbursement-v1.0.2-source.tar.gz"
                self.assertFalse(archive.exists(), "a rejected source tree must not publish an archive")

    def test_source_package_rejects_protected_files_inside_allowed_directories(self):
        protected = (
            "static/.env",
            "static/.env.production",
            "templates/app-secret",
            "resources/customer.sqlite-wal",
            "tests/private-user-export.pdf",
        )
        for relative in protected:
            with self.subTest(protected=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _make_release_fixture(root)
                (root / "static" / "app.js").write_text("console.log('safe');\n", encoding="utf-8")
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"private")

                result = subprocess.run(
                    ["sh", str(root / "scripts" / "package-synology.sh"), "--source"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0, "packaging must fail closed on protected files")
                self.assertRegex(result.stderr, r"受保护文件|未批准的交付文件")
                archive = root / "dist" / "junmu-reimbursement-v1.0.2-source.tar.gz"
                self.assertFalse(archive.exists(), "a rejected source tree must not publish an archive")

    def test_source_package_renders_app_version_from_version_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_release_fixture(root, version="v1.2.3", env_version="v9.9.9")

            result = subprocess.run(
                ["sh", str(root / "scripts" / "package-synology.sh"), "--source"],
                cwd=root,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            archive = root / "dist" / "junmu-reimbursement-v1.2.3-source.tar.gz"
            packaged_env = _archive_text(archive, ".env.example")
            self.assertIn("APP_VERSION=v1.2.3\n", packaged_env)
            self.assertNotIn("APP_VERSION=v9.9.9", packaged_env)

    def test_source_package_omits_macos_metadata_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_release_fixture(root)
            (root / "static" / "app.js").write_text("console.log('safe');\n", encoding="utf-8")
            (root / "static" / "._app.js").write_bytes(b"private metadata")

            result = subprocess.run(
                ["sh", str(root / "scripts" / "package-synology.sh"), "--source"],
                cwd=root,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            archive = root / "dist" / "junmu-reimbursement-v1.0.2-source.tar.gz"
            names = _archive_relative_names(archive)
            self.assertFalse(
                any(part.startswith("._") for name in names for part in name.split("/")),
                sorted(names),
            )
            with tarfile.open(archive, "r:gz") as handle:
                metadata_headers = {
                    member.name: sorted(
                        key for key in member.pax_headers
                        if "xattr" in key.lower() or "acl" in key.lower()
                    )
                    for member in handle.getmembers()
                    if any("xattr" in key.lower() or "acl" in key.lower() for key in member.pax_headers)
                }
            self.assertEqual(metadata_headers, {})

    def test_source_package_supports_tar_without_format_option(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _make_release_fixture(root)
            real_tar = shutil.which("tar")
            self.assertIsNotNone(real_tar)
            tool_dir = root / "bin"
            tool_dir.mkdir()
            tar_wrapper = tool_dir / "tar"
            tar_wrapper.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$TAR_TEST_LOG\"\n"
                "case \"${1:-}\" in --help) printf '%s\\n' 'BusyBox tar'; exit 0;; esac\n"
                "case \" $* \" in *' --format '*) exit 64;; esac\n"
                f"exec {shlex.quote(real_tar)} \"$@\"\n",
                encoding="utf-8",
            )
            tar_wrapper.chmod(0o755)
            log = root / "tar-calls"
            environment = {
                **os.environ,
                "PATH": f"{tool_dir}:{os.environ['PATH']}",
                "TAR_TEST_LOG": str(log),
            }

            result = subprocess.run(
                ["sh", str(root / "scripts" / "package-synology.sh"), "--source"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            archive = root / "dist" / "junmu-reimbursement-v1.0.2-source.tar.gz"
            self.assertTrue(archive.is_file())
            actual_calls = [line for line in log.read_text().splitlines() if line != "--help"]
            self.assertTrue(actual_calls)
            self.assertFalse(any("--format" in line for line in actual_calls), actual_calls)

    def test_versioned_release_scripts_use_versioned_identity_and_artifacts(self):
        version = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "v1.0.2")

        release_vars = APP_DIR / "scripts" / "release-vars.sh"
        self.assertTrue(release_vars.is_file(), "release-vars.sh is required")
        release_vars_text = release_vars.read_text(encoding="utf-8")
        self.assertRegex(release_vars_text, r"(?m)^\s*(?:export\s+)?IMAGE_REPO\s*=\s*['\"]?junmu-reimbursement['\"]?\s*$")
        self.assertRegex(release_vars_text, r"(?m)^\s*(?:export\s+)?IMAGE_TAG\s*=.*(?:IMAGE_REPO|VERSION)")
        self.assertRegex(release_vars_text, r"(?m)^\s*(?:export\s+)?CONTAINER_NAME\s*=.*(?:IMAGE_REPO|VERSION)")
        self.assertRegex(release_vars_text, r"\bVERSION\b")
        self.assertRegex(release_vars_text, r"v[0-9]+\\?\.[0-9]+\\?\.[0-9]+")

        scripts = {}
        for name in ("build-docker.sh", "export-docker.sh", "package-synology.sh"):
            path = APP_DIR / "scripts" / name
            self.assertTrue(path.is_file(), str(path))
            scripts[name] = path.read_text(encoding="utf-8")

        for name, text in scripts.items():
            with self.subTest(script=name):
                self.assertTrue(
                    re.search(r"(?m)^\s*(?:\.|source)\s+[^#\n]*release-vars\.sh(?:\s|$)", text),
                    "release script must source release-vars.sh",
                )
                self.assertRegex(text, r"\b(?:VERSION|IMAGE_REPO|IMAGE_TAG)\b")
                self.assertNotRegex(text, r"(?i)\blatest\b")
                self.assertNotIn("reimbursement-system", text)

        build_commands = [
            words for words in _docker_commands(scripts["build-docker.sh"])
            if words[1:3] == ["buildx", "build"]
        ]
        self.assertTrue(build_commands, "build script must invoke docker buildx build")
        build = build_commands[0]
        self.assertIn("--platform", build)
        self.assertEqual(build[build.index("--platform") + 1], "linux/amd64")
        self.assertIn("--build-arg", build)
        build_arg = build[build.index("--build-arg") + 1]
        self.assertTrue(build_arg.startswith("APP_VERSION="))
        self.assertTrue(_has_variable(build_arg, "VERSION"))
        self.assertIn("--tag", build)
        self.assertTrue(_has_variable(build[build.index("--tag") + 1], "IMAGE_TAG"))
        self.assertNotIn("latest", " ".join(build).lower())

        inspect_commands = [
            words for words in _docker_commands(scripts["export-docker.sh"])
            if words[1:3] == ["image", "inspect"]
        ]
        self.assertTrue(inspect_commands, "export script must inspect the versioned image")
        self.assertTrue(any(_has_variable(token, "IMAGE_TAG") for token in inspect_commands[0]))
        save_commands = [
            words for words in _docker_commands(scripts["export-docker.sh"])
            if words[1:2] == ["save"]
        ]
        self.assertTrue(save_commands, "export script must save the versioned image")
        self.assertTrue(any(_has_variable(token, "IMAGE_TAG") for token in save_commands[0]))

        _assert_versioned_archive(self, scripts["export-docker.sh"], "linux-amd64")
        _assert_versioned_archive(self, scripts["package-synology.sh"], "synology")
        _assert_versioned_archive(self, scripts["package-synology.sh"], "source")
        self.assertRegex(
            "\n".join(_logical_shell_lines(scripts["package-synology.sh"])),
            r"(?:IMAGE_ARCHIVE|IMAGE)\s*=.*(?:junmu-reimbursement|IMAGE_REPO).*VERSION.*linux-amd64\.tar\.gz",
        )

    def test_release_vars_rejects_invalid_versions_and_exports_values(self):
        source = APP_DIR / "scripts" / "release-vars.sh"
        self.assertTrue(source.is_file(), "release-vars.sh is required")

        def run_with_version(version_text):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "scripts").mkdir()
                copied = root / "scripts" / "release-vars.sh"
                copied.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                copied.chmod(0o755)
                (root / "VERSION").write_text(version_text, encoding="utf-8")
                probe = (
                    f'set -eu; . "{copied}"; '
                    'printf "%s\\n" "VERSION=$VERSION" "IMAGE_REPO=$IMAGE_REPO" '
                    '"IMAGE_TAG=$IMAGE_TAG" "CONTAINER_NAME=$CONTAINER_NAME"'
                )
                return subprocess.run(
                    ["sh", "-c", probe],
                    cwd=root,
                    capture_output=True,
                    text=True,
                )

        valid = run_with_version("v1.0.2\n")
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(
            valid.stdout.splitlines(),
            [
                "VERSION=v1.0.2",
                "IMAGE_REPO=junmu-reimbursement",
                "IMAGE_TAG=junmu-reimbursement:v1.0.2",
                "CONTAINER_NAME=junmu-reimbursement-v1.0.2",
            ],
        )
        for invalid in ("", "\n", "v1.0.2\nv1.0.3\n", "1.0.2\n", "v1.0\n", "v1.0.2 trailing\n"):
            with self.subTest(version=repr(invalid)):
                result = run_with_version(invalid)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_export_rejects_image_when_oci_version_label_does_not_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            for relative in ("VERSION", "scripts/release-vars.sh", "scripts/export-docker.sh"):
                shutil.copy2(APP_DIR / relative, root / relative)
            docker = root / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$EXPORT_TEST_LOG\"\n"
                "case \"$*\" in\n"
                "  *org.opencontainers.image.version*) printf '%s\\n' \"$FAKE_IMAGE_LABEL\";;\n"
                "  *Architecture*) printf '%s\\n' 'linux/amd64';;\n"
                "  save*) : > \"$3\";;\n"
                "esac\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            log = root / "calls"
            environment = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "EXPORT_TEST_LOG": str(log),
                "FAKE_IMAGE_LABEL": "v9.9.9",
            }

            result = subprocess.run(
                ["sh", str(root / "scripts" / "export-docker.sh")],
                cwd=root,
                env=environment,
                capture_output=True,
            )

            try:
                stderr = result.stderr.decode("utf-8")
            except UnicodeDecodeError:
                self.fail(f"export script emitted non-UTF-8 stderr: {result.stderr!r}")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("v9.9.9", stderr)
            self.assertFalse(any(call.startswith("save ") for call in log.read_text().splitlines()))

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
                         "/volume1/docker/baoxiao/data", "/volume1/docker/baoxiao/backups",
                         "PUID", "PGID", "chown", "局域网",
                         "--restore", "回滚", "防火墙", "purge_claim"):
            self.assertIn(required, readme)

    def test_readme_stops_v101_project_before_starting_fixed_v102_project(self):
        readme = (APP_DIR / "README_部署说明.md").read_text(encoding="utf-8")
        heading = "### 从 v1.0.1 升级"
        self.assertIn(heading, readme)
        section = readme.split(heading, 1)[1].split("\n### ", 1)[0]
        stop_index = section.find("docker compose stop reimbursement")
        backup_index = section.find("reimbursement python backup.py")
        verify_index = section.find("backup.py --verify")
        down_index = section.find("docker compose down --remove-orphans")
        port_index = section.find("8800", down_index)
        start_index = section.find("docker compose up -d")
        self.assertTrue(
            -1 < stop_index < backup_index < verify_index < down_index < port_index < start_index,
            section,
        )
        self.assertIn("备份", section)
        self.assertIn("旧版目录", section)
        self.assertIn("失败", section)

    def test_local_launcher_probes_healthz(self):
        launcher = (APP_DIR / "start_reimbursement_tool.sh").read_text(encoding="utf-8")
        self.assertIn("/healthz", launcher)
        self.assertNotIn("/api/health", launcher)

    def test_shell_scripts_have_valid_syntax(self):
        paths = [APP_DIR / "start_reimbursement_tool.sh", APP_DIR / "start_reimbursement_tool.command"]
        paths += [APP_DIR / "scripts" / name for name in (
            "backup.sh", "build-docker.sh", "export-docker.sh", "package-synology.sh", "release-vars.sh")]
        for path in paths:
            with self.subTest(script=path.name):
                self.assertTrue(path.is_file(), str(path))
                result = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("set -eu", path.read_text())

    def test_backup_restarts_and_checks_health_when_backup_fails(self):
        script_path = APP_DIR / "scripts" / "backup.sh"
        self.assertTrue(script_path.is_file(), "backup script is required")
        script_text = script_path.read_text(encoding="utf-8")
        self.assertRegex(script_text, r"docker\s+compose\s+stop\s+junmu-reimbursement\b")
        self.assertRegex(script_text, r"docker\s+compose\s+run\b[^\n]*\bjunmu-reimbursement\b")
        self.assertRegex(script_text, r"docker\s+compose\s+up\s+-d\s+--wait\s+--wait-timeout\s+120\s+junmu-reimbursement\b")
        self.assertNotRegex(script_text, r"(?<!junmu-)\breimbursement(?:-system)?\b")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / "docker"
            docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BACKUP_TEST_LOG\"\n"
                              "case \"$*\" in *'run --rm'*) exit 7;; esac\n")
            docker.chmod(0o755)
            log = root / "calls"
            environment = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "BACKUP_TEST_LOG": str(log)}
            result = subprocess.run(["sh", str(script_path)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, result.stderr)
            calls = log.read_text().splitlines()
            self.assertIn("stop junmu-reimbursement", calls[0])
            self.assertIn("junmu-reimbursement", calls[1])
            self.assertIn("python backup.py --data-dir /data --backup-dir /backups", calls[1])
            self.assertIn("up -d --wait", calls[2])
            self.assertIn("--wait-timeout 120", calls[2])
            self.assertIn("junmu-reimbursement", calls[2])
            self.assertNotIn("reimbursement-system", script_text)

    def test_backup_reports_restart_failure_after_a_failed_backup(self):
        script = APP_DIR / "scripts" / "backup.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$BACKUP_TEST_LOG\"\n"
                "case \"$*\" in\n"
                "  *'run --rm'*) exit 7;;\n"
                "  *'up -d --wait --wait-timeout 120 junmu-reimbursement'*) exit 9;;\n"
                "esac\n"
            )
            docker.chmod(0o755)
            log = root / "calls"
            environment = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "BACKUP_TEST_LOG": str(log)}
            result = subprocess.run(["sh", str(script)], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("健康", result.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(len(calls), 5)
            self.assertEqual(sum("up -d --wait" in call for call in calls), 3)
            self.assertIn("junmu-reimbursement", calls[-1])

    def test_backup_defers_process_group_signal_until_restart_finishes(self):
        script = APP_DIR / "scripts" / "backup.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "from pathlib import Path\n"
                "import signal\n"
                "import sys\n"
                "args = ' '.join(sys.argv[1:])\n"
                "log = Path(os.environ['BACKUP_TEST_LOG'])\n"
                "with log.open('a', encoding='utf-8') as handle:\n"
                "    handle.write(f'start {args}\\n')\n"
                "if 'run --rm' in args:\n"
                "    raise SystemExit(7)\n"
                "if 'up -d --wait' in args:\n"
                "    state = Path(os.environ['BACKUP_SIGNAL_STATE'])\n"
                "    if not state.exists():\n"
                "        state.touch()\n"
                "        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\n"
                "        os.killpg(0, signal.SIGTERM)\n"
                "    with log.open('a', encoding='utf-8') as handle:\n"
                "        handle.write('restart-complete\\n')\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            log = root / "calls"
            state = root / "signal-sent"
            environment = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "BACKUP_TEST_LOG": str(log),
                "BACKUP_SIGNAL_STATE": str(state),
            }

            result = subprocess.run(
                ["sh", str(script)],
                env=environment,
                capture_output=True,
                text=True,
                start_new_session=True,
                timeout=5,
            )

            self.assertEqual(result.returncode, 7, result.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(sum("up -d --wait" in call for call in calls), 2)
            self.assertIn("restart-complete", calls)


if __name__ == "__main__":
    unittest.main()
