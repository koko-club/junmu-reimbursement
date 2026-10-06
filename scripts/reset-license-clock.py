#!/usr/bin/env python3
"""Clear one account's license clock watermark after correcting server time."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import Database  # noqa: E402
from license import InvalidLicense  # noqa: E402
from license_service import LicenseService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="校正服务器时间后重置指定账号的授权时间记录")
    parser.add_argument("serial", help="用户序列号")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("APP_DATA_DIR", "data")))
    args = parser.parse_args()
    db = Database(args.data_dir / "database" / "app.db")
    if not db.path.is_file() or db.path.is_symlink():
        print("数据库文件不存在或不是普通文件", file=sys.stderr)
        return 1
    try:
        LicenseService(db).reset_clock(args.serial)
    except (InvalidLicense, OSError) as error:
        print(f"重置失败：{error}", file=sys.stderr)
        return 1
    print("授权时间记录已重置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
