#!/usr/bin/env python3
"""Issue a 365-day Pro code on the owner's Mac. Keep the private key off servers."""

from __future__ import annotations

import argparse
from pathlib import Path
import stat
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from license import production_public_key, sign_license  # noqa: E402


DEFAULT_KEY = Path.home() / "Library/Application Support/JunmuReimbursement/issuer-private.pem"


def issue(key_path: Path, serial: str) -> str:
    if key_path.is_symlink() or not key_path.is_file():
        raise ValueError("私钥文件不存在或不是普通文件")
    mode = key_path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("私钥权限过宽，请执行 chmod 600")
    loaded = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ValueError("需要 Ed25519 私钥")
    actual_public = loaded.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    expected_public = production_public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if actual_public != expected_public:
        raise ValueError("私钥与应用公钥不匹配")
    return sign_license(loaded, serial)


def main() -> int:
    parser = argparse.ArgumentParser(description="为用户序列号签发365天 Pro 授权码")
    parser.add_argument("serial", help="用户个人中心显示的32位序列号")
    parser.add_argument("--key", type=Path, default=DEFAULT_KEY, help="Mac 上的私钥路径")
    arguments = parser.parse_args()
    try:
        print(issue(arguments.key, arguments.serial))
    except (OSError, ValueError, TypeError) as error:
        print(f"签发失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
