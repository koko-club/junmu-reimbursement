#!/usr/bin/env python3
"""Build a relocatable deployment archive for the reimbursement form app.

The default archive includes the current macOS arm64 Python and LibreOffice
runtimes when they are available. ``--without-runtime`` creates a small source
archive for machines where Python/LibreOffice are installed separately.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


APP_FILES = (
    "app.py",
    "generator.py",
    "office.py",
    "validation.py",
    "config.json",
    "requirements.txt",
    "README_部署说明.md",
    "build_deployment_package.py",
    "start_reimbursement_tool.command",
    "start_reimbursement_tool.sh",
    "start_reimbursement_tool.bat",
)
APP_DIRS = ("templates", "static", "resources")
DEFAULT_PYTHON_RUNTIME = Path(
    "/Users/koko/.cache/codex-runtimes/codex-primary-runtime/dependencies/python"
)
DEFAULT_LIBREOFFICE_RUNTIME = Path(
    "/Users/koko/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/libreoffice-headless"
)


def _copy_required(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"required file is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_tree(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"required directory is missing: {source}")
    shutil.copytree(source, target, dirs_exist_ok=True)


def _copy_portable_python(source: Path, target: Path) -> None:
    """Copy only CPython stdlib plus the two packages used by the app."""
    if not (source / "bin" / "python3.12").is_file():
        raise FileNotFoundError(f"portable Python runtime is missing: {source}")
    (target / "bin").mkdir(parents=True, exist_ok=True)
    for executable in ("python3.12", "python3"):
        source_executable = source / "bin" / executable
        if source_executable.is_file():
            shutil.copy2(source_executable, target / "bin" / executable)
    _copy_required(source / "lib" / "libpython3.12.dylib", target / "lib" / "libpython3.12.dylib")

    stdlib_source = source / "lib" / "python3.12"
    stdlib_target = target / "lib" / "python3.12"
    shutil.copytree(
        stdlib_source,
        stdlib_target,
        ignore=shutil.ignore_patterns("site-packages", "__pycache__"),
    )
    package_source = stdlib_source / "site-packages"
    package_target = stdlib_target / "site-packages"
    package_target.mkdir(parents=True, exist_ok=True)
    for name in (
        "openpyxl",
        "openpyxl-3.1.5.dist-info",
        "et_xmlfile",
        "et_xmlfile-2.0.0.dist-info",
        "PIL",
        "pillow-12.3.0.dist-info",
    ):
        _copy_tree(package_source / name, package_target / name)


def _copy_portable_libreoffice(source: Path, target: Path) -> None:
    _copy_tree(source, target)


def _manifest(root: Path, include_runtime: bool) -> dict:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append({"path": path.relative_to(root).as_posix(), "sha256": digest})
    return {
        "name": "差旅报销单网页工具",
        "created": date.today().isoformat(),
        "platform": "macOS arm64" if include_runtime else "source",
        "portable_runtime_included": include_runtime,
        "files": files,
    }


def _write_zip(source_root: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source_root).as_posix()
            info = ZipInfo(relative)
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.compress_type = ZIP_DEFLATED
            # Preserve executable bits for shell launchers after extraction.
            mode = path.stat().st_mode
            info.external_attr = (mode & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())


def build_package(
    project_dir: Path,
    output_dir: Path,
    *,
    include_runtime: bool = True,
    python_runtime: Path = DEFAULT_PYTHON_RUNTIME,
    libreoffice_runtime: Path = DEFAULT_LIBREOFFICE_RUNTIME,
) -> Path:
    """Build and return a zip archive without mutating the project files."""
    project_dir = Path(project_dir).resolve()
    output_dir = Path(output_dir).resolve()
    suffix = "部署包" if include_runtime else "源码包"
    package_name = f"差旅报销单网页工具-{suffix}-{date.today().strftime('%Y%m%d')}"
    archive_path = output_dir / f"{package_name}.zip"
    with tempfile.TemporaryDirectory(prefix="reimbursement-package-") as temp_name:
        package_root = Path(temp_name) / package_name
        package_root.mkdir(parents=True)
        for relative in APP_FILES:
            _copy_required(project_dir / relative, package_root / relative)
        for relative in APP_DIRS:
            _copy_tree(project_dir / relative, package_root / relative)
        (package_root / "generated").mkdir()
        (package_root / "generated" / ".gitkeep").write_text("", encoding="utf-8")

        if include_runtime:
            _copy_portable_python(Path(python_runtime), package_root / "runtime" / "python")
            _copy_portable_libreoffice(Path(libreoffice_runtime), package_root / "runtime" / "libreoffice")

        manifest = _manifest(package_root, include_runtime)
        (package_root / "DEPLOYMENT_MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _write_zip(package_root, archive_path)
    return archive_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument("--without-runtime", action="store_true", help="build a small source-only archive")
    parser.add_argument("--python-runtime", type=Path, default=DEFAULT_PYTHON_RUNTIME)
    parser.add_argument("--libreoffice-runtime", type=Path, default=DEFAULT_LIBREOFFICE_RUNTIME)
    args = parser.parse_args()
    archive = build_package(
        Path(__file__).resolve().parent,
        args.output_dir,
        include_runtime=not args.without_runtime,
        python_runtime=args.python_runtime,
        libreoffice_runtime=args.libreoffice_runtime,
    )
    print(archive)


if __name__ == "__main__":
    main()
