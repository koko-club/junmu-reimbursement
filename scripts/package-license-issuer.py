#!/usr/bin/env python3
"""Build a relocatable, offline macOS arm64 Pro license issuer ZIP."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import platform
import stat
import sys
import tempfile
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from license import production_public_key  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "pro-license-issuer-macos-arm64"
OUTPUT = ROOT / "dist" / f"{PACKAGE}.zip"
PRIVATE_KEY = Path.home() / "Library/Application Support/JunmuReimbursement/issuer-private.pem"
SITE_PACKAGES = ROOT / ".venv" / "lib" / "python3.12" / "site-packages"
VENDOR_NAMES = ("cryptography", "cffi", "pycparser")

RUNNER = '''from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "vendor"))
sys.path.insert(0, str(root))
runpy.run_path(str(root / "scripts" / "issue-license.py"), run_name="__main__")
'''

COMMAND = '''#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v python3.12 >/dev/null 2>&1; then
    python_bin=$(command -v python3.12)
elif [ -x /opt/homebrew/opt/python@3.12/bin/python3.12 ]; then
    python_bin=/opt/homebrew/opt/python@3.12/bin/python3.12
else
    echo "需要先安装 Python 3.12（macOS Apple Silicon）。" >&2
    exit 1
fi
exec "$python_bin" -I "$root/run.py" --key "$root/issuer-private.pem" "$@"
'''

README = '''Pro 授权码签发工具（macOS Apple Silicon）

此目录可以放在 Mac 上任意位置；无需进入报销系统项目目录，也无需联网安装 Python 包。
电脑需安装 Python 3.12。此包附带签发所需的第三方 Python 模块，不附带 Python 解释器。

在终端运行（将路径替换为解压后的实际位置）：

    sh "/路径/pro-license-issuer-macos-arm64/issue-license.command" 用户的32位序列号

例如，在此目录内运行：

    sh issue-license.command 8ae40d905bb6597d603c22f84dc6d1e8

命令会输出一行授权码。仅将授权码发给对应用户，由用户在个人中心导入。
每次授权从签发时刻起有效 365 天。

签发私钥在本目录的 issuer-private.pem；工具默认使用它。可用
--key /安全路径/issuer-private.pem 指定其他私钥。私钥文件权限必须为 600，
且必须与报销系统内置公钥匹配。如果解压后提示权限过宽，执行：

    chmod 600 issuer-private.pem

此 ZIP 包含签发私钥；任何拿到 ZIP 的人都能签发 Pro 授权码。请仅供授权人
自己保管，不要发送给被授权用户，也不要上传到公开或共享目录。
此 ZIP 内的第三方库仅适用 macOS arm64。
'''


def add_bytes(archive: zipfile.ZipFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = zipfile.ZipInfo(f"{PACKAGE}/{name}", date_time=datetime.now().timetuple()[:6])
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def add_file(archive: zipfile.ZipFile, source: Path, name: str, mode: int = 0o644) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"打包来源不是普通文件：{source}")
    add_bytes(archive, name, source.read_bytes(), mode)


def main() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64" or sys.version_info[:2] != (3, 12):
        raise SystemExit("请在装有 Python 3.12 的 macOS Apple Silicon 上打包")
    if not SITE_PACKAGES.is_dir():
        raise SystemExit(f"找不到依赖目录：{SITE_PACKAGES}")
    vendor_paths = []
    for name in VENDOR_NAMES:
        package_dir = SITE_PACKAGES / name
        distribution = list(SITE_PACKAGES.glob(f"{name}-*.dist-info"))
        if not package_dir.is_dir() or len(distribution) != 1:
            raise SystemExit(f"缺少唯一的 {name} 安装包或元数据")
        vendor_paths.extend((package_dir, distribution[0]))
    backend = list(SITE_PACKAGES.glob("_cffi_backend.cpython-312-darwin.so"))
    if len(backend) != 1:
        raise SystemExit("缺少 macOS Python 3.12 CFFI 后端")
    vendor_paths.extend(backend)
    if PRIVATE_KEY.is_symlink() or not PRIVATE_KEY.is_file():
        raise SystemExit(f"找不到普通私钥文件：{PRIVATE_KEY}")
    if stat.S_IMODE(PRIVATE_KEY.stat().st_mode) & 0o077:
        raise SystemExit("私钥权限过宽，请先执行 chmod 600")
    loaded = serialization.load_pem_private_key(PRIVATE_KEY.read_bytes(), password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise SystemExit("签发私钥不是 Ed25519 格式")
    actual_public = loaded.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    expected_public = production_public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if actual_public != expected_public:
        raise SystemExit("签发私钥与应用公钥不匹配")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    pending: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{PACKAGE}-", suffix=".tmp", dir=OUTPUT.parent, delete=False) as output:
            pending = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            with zipfile.ZipFile(output, "w") as archive:
                add_file(archive, ROOT / "license.py", "license.py")
                add_file(archive, ROOT / "scripts" / "issue-license.py", "scripts/issue-license.py")
                add_file(archive, PRIVATE_KEY, "issuer-private.pem", 0o600)
                add_bytes(archive, "run.py", RUNNER.encode())
                add_bytes(archive, "issue-license.command", COMMAND.encode(), 0o755)
                add_bytes(archive, "README.txt", README.encode())
                for path in vendor_paths:
                    for file in (path.rglob("*") if path.is_dir() else (path,)):
                        if file.is_dir() or "__pycache__" in file.parts or file.suffix == ".pyc":
                            continue
                        add_file(archive, file, f"vendor/{file.relative_to(SITE_PACKAGES)}")
        pending.replace(OUTPUT)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
