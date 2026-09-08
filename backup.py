"""Offline, manifest-verified backups of the application's owned data only."""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tarfile
import tempfile
from uuid import UUID

from database import Database


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_REQUIRED = {"database/app.db", "app-secret"}


def _checked_path(value: Path | str) -> Path:
    if not str(value).strip() or ".." in Path(value).parts:
        raise ValueError("路径必须明确且不能包含 ..")
    path = Path(os.path.abspath(value))
    if path in {Path("/"), Path.home(), Path.cwd()}:
        raise ValueError("不能使用根目录、主目录或项目目录作为数据目标")
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise ValueError(f"路径不能包含符号链接: {component}")
    return path


def _relative_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
            or path.as_posix() != name or any(ord(char) < 32 for char in name)):
        raise ValueError("归档路径无效")
    return path


def _owned_paths(snapshot: Path) -> set[str]:
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.execute("PRAGMA trusted_schema = OFF")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("数据库完整性检查失败")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("数据库文件归属检查失败")
        rows = connection.execute(
            "SELECT id, user_id, xlsx_path, pdf_path, purge_claim FROM reimbursements"
        ).fetchall()
    result: set[str] = set()
    for record_id, user_id, xlsx, pdf, claim in rows:
        if claim is not None:
            raise ValueError("存在未完成的 purge_claim 清理任务；请启动服务完成清理后停服重试，勿手工删除记录或隔离目录")
        record_uuid = UUID(record_id)
        if str(record_uuid) != record_id or record_uuid.version != 4 or type(user_id) is not int or user_id < 1:
            raise ValueError("报销记录归属无效")
        for name, suffix in ((xlsx, ".xlsx"), (pdf, ".pdf")):
            path = _relative_name(name)
            if (len(path.parts) < 4 or path.parts[:3] != ("users", str(user_id), record_id)
                    or path.suffix.lower() != suffix or name in result
                    or any(part.startswith(".reimbursement-") for part in path.parts)):
                raise ValueError("报销文件路径超出所属用户记录")
            result.add(name)
    return result


@contextmanager
def _open_file(root: Path, name: str):
    parts = _relative_name(name).parts
    descriptors = [os.open(root, _DIRECTORY_FLAGS)]
    try:
        for component in parts[:-1]:
            descriptors.append(os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptors[-1]))
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=descriptors[-1])
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ValueError("备份输入必须是无硬链接的普通文件")
            yield stream
            after = os.fstat(stream.fileno())
            current = os.stat(parts[-1], dir_fd=descriptors[-1], follow_symlinks=False)
            if (not os.path.samestat(before, current) or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns or after.st_nlink != 1):
                raise ValueError("备份期间数据发生变化；请停服后重试")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _copy_and_hash(source, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256()
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest()


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def create_backup(data_dir: Path, backup_dir: Path, now: datetime | None = None) -> Path:
    """Create an exclusive archive. The application and other data writers must be stopped."""
    source = _checked_path(data_dir)
    destination = _checked_path(backup_dir)
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("数据目录与备份目录不能重叠")
    if not source.is_dir():
        raise FileNotFoundError(source)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("备份时间必须含时区")
    instant = instant.astimezone(timezone.utc)
    archive_name = f"reimbursement-{instant.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    _checked_path(destination)
    archive = destination / archive_name
    if archive.exists() or archive.is_symlink():
        raise FileExistsError(archive)
    with tempfile.TemporaryDirectory(prefix=".backup-stage-", dir=destination) as temporary:
        stage = Path(temporary)
        # SQLite's backup API consumes WAL state; validate its sidecars before opening it.
        for suffix in ("-wal", "-shm", "-journal"):
            relative = "database/app.db" + suffix
            if (source / relative).exists() or (source / relative).is_symlink():
                with _open_file(source, relative):
                    pass
        with _open_file(source, "database/app.db"):
            Database(source / "database/app.db").backup(stage / "database" / "app.db")
        owned = _owned_paths(stage / "database" / "app.db")
        hashes = {"database/app.db": _digest(stage / "database" / "app.db")}
        for name in sorted(owned | {"app-secret"}):
            with _open_file(source, name) as stream:
                hashes[name] = _copy_and_hash(stream, stage / name)
        if (stage / "app-secret").stat().st_size != 32:
            raise ValueError("app-secret 必须是原始 32 字节密钥")
        manifest = {"version": 1, "created_at": instant.isoformat(), "files": hashes}
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=True, sort_keys=True), encoding="utf-8")
        with tempfile.NamedTemporaryFile(prefix=".backup-", suffix=".tmp", dir=destination, delete=False) as output:
            pending = Path(output.name)
            try:
                os.fchmod(output.fileno(), 0o600)
                with tarfile.open(fileobj=output, mode="w:gz") as tar:
                    for name in sorted(hashes.keys() | {"manifest.json"}):
                        info = tar.gettarinfo(stage / name, arcname=name)
                        info.mode, info.uid, info.gid = 0o600, 0, 0
                        info.uname = info.gname = ""
                        with (stage / name).open("rb") as stream:
                            tar.addfile(info, stream)
                output.flush()
                os.fsync(output.fileno())
                os.link(pending, archive)
            finally:
                pending.unlink(missing_ok=True)
    return archive


def _extract_verified(archive: Path, stage: Path) -> dict:
    hashes = {}
    with _open_file(archive.parent, archive.name) as stream, tarfile.open(fileobj=stream, mode="r:gz") as tar:
        for member in tar:
            name = member.name
            path = _relative_name(name)
            if not member.isfile() or name in hashes:
                raise ValueError("归档含重复路径、链接或非普通文件")
            if name not in _REQUIRED | {"manifest.json"} and (len(path.parts) < 4 or path.parts[0] != "users"):
                raise ValueError("归档含未授权路径")
            if name == "manifest.json" and member.size > 16 * 1024 * 1024:
                raise ValueError("备份清单过大")
            with tar.extractfile(member) as content:
                hashes[name] = _copy_and_hash(content, stage / name)
    if not _REQUIRED | {"manifest.json"} <= hashes.keys():
        raise ValueError("归档缺少数据库、密钥或清单")
    try:
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ValueError("备份清单不是有效 JSON") from error
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("备份清单格式不支持")
    del hashes["manifest.json"]
    if manifest["files"] != hashes or any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes.values()):
        raise ValueError("SHA256 清单校验失败")
    if (stage / "app-secret").stat().st_size != 32:
        raise ValueError("app-secret 长度无效")
    if _owned_paths(stage / "database" / "app.db") | _REQUIRED != hashes.keys():
        raise ValueError("清单与数据库登记文件不一致")
    (stage / "manifest.json").unlink()
    return manifest


def verify_backup(archive: Path) -> dict:
    archive = _checked_path(archive)
    with tempfile.TemporaryDirectory(prefix="reimbursement-verify-") as temporary:
        return _extract_verified(archive, Path(temporary))


def restore_backup(archive: Path, data_dir: Path) -> Path:
    """Verify and restore into a new directory; never merge with existing data."""
    archive = _checked_path(archive)
    destination = _checked_path(data_dir)
    if destination.exists():
        raise FileExistsError(destination)
    if destination in archive.parents:
        raise ValueError("恢复目录不能包含备份归档")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "data"
        stage.mkdir(mode=0o700)
        _extract_verified(archive, stage)
        # Reserve the destination exclusively so even an empty pre-existing directory is preserved.
        destination.mkdir(mode=0o700)
        try:
            stage.rename(destination)
        except BaseException:
            destination.rmdir()
            raise
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="停服备份、SHA256 校验与恢复（操作期间禁止其他进程写入数据）")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--verify", type=Path, metavar="ARCHIVE")
    operation.add_argument("--restore", type=Path, metavar="ARCHIVE")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    try:
        if args.verify:
            verify_backup(args.verify)
            print("备份校验通过")
        elif args.restore and args.data_dir:
            print(restore_backup(args.restore, args.data_dir))
        elif not args.restore and args.data_dir and args.backup_dir:
            print(create_backup(args.data_dir, args.backup_dir))
        else:
            parser.error("备份需要 --data-dir 和 --backup-dir；恢复需要 --restore 和 --data-dir")
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as error:
        parser.exit(1, f"操作失败: {error}\n")


if __name__ == "__main__":
    main()
