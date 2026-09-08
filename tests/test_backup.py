from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

from database import Database


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / "data"
        self.destination = self.root / "backups"
        self.database = Database(self.data / "database" / "app.db")
        self.database.migrate()
        (self.data / "app-secret").write_bytes(b"s" * 32)
        with self.database.transaction() as connection:
            connection.execute("""INSERT INTO users(
                id, username, username_key, password_hash, password_salt,
                password_params, real_name, department, role, status,
                created_at, updated_at
            ) VALUES (1, 'alice', 'alice', X'00', X'00', '{}', 'Alice', 'IT',
                'user', 'active', 'created', 'updated')""")
        self.now = datetime(2026, 9, 9, 8, 7, 6, tzinfo=timezone.utc)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("backup"), "backup module is required")
        import backup
        return backup

    def add_record(self, number=1, deleted=False):
        record_id = f"10000000-0000-4000-8000-{number:012d}"
        relative = Path("users") / "1" / record_id
        directory = self.data / relative
        directory.mkdir(parents=True)
        for suffix in ("xlsx", "pdf"):
            (directory / f"claim.{suffix}").write_bytes(f"{number}-{suffix}".encode())
        with self.database.transaction() as connection:
            connection.execute("""INSERT INTO reimbursements(
                id, user_id, display_name, xlsx_path, pdf_path, created_at, deleted_at
            ) VALUES (?, 1, 'claim.xlsx', ?, ?, 'created', ?)""", (
                record_id, (relative / "claim.xlsx").as_posix(),
                (relative / "claim.pdf").as_posix(), "deleted" if deleted else None,
            ))
        return relative

    def test_backup_snapshot_manifest_owned_active_and_trash_files(self):
        backup = self.module()
        active = self.add_record()
        trash = self.add_record(2, deleted=True)
        (self.data / "tmp").mkdir()
        (self.data / "tmp" / "secret-orphan").write_text("not owned")
        orphan = self.data / "users" / "1" / "orphan"
        orphan.mkdir()
        (orphan / "orphan.pdf").write_bytes(b"orphan")
        (self.data / active / ".reimbursement-cleanup-empty").mkdir()
        with mock.patch.object(Database, "backup", autospec=True, side_effect=Database.backup) as snapshot:
            archive = backup.create_backup(self.data, self.destination, now=self.now)
        self.assertEqual(snapshot.call_count, 1)
        self.assertEqual(archive.name, "reimbursement-20260909T080706Z.tar.gz")
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        with tarfile.open(archive) as tar:
            expected = {"database/app.db", "app-secret"} | {
                (relative / f"claim.{suffix}").as_posix()
                for relative in (active, trash) for suffix in ("xlsx", "pdf")
            }
            self.assertEqual(set(tar.getnames()), expected | {"manifest.json"})
            manifest = json.load(tar.extractfile("manifest.json"))
            self.assertEqual(set(manifest["files"]), expected)
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(tar.extractfile(name).read()).hexdigest(), digest)
        self.assertEqual(list(self.destination.iterdir()), [archive])

    def test_restore_roundtrip_keeps_account_secret_and_trash(self):
        backup = self.module()
        self.add_record(deleted=True)
        archive = backup.create_backup(self.data, self.destination, now=self.now)
        restored = self.root / "restored"
        backup.restore_backup(archive, restored)
        self.assertEqual((restored / "app-secret").read_bytes(), b"s" * 32)
        connection = sqlite3.connect(restored / "database" / "app.db")
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("SELECT username FROM users").fetchone()[0], "alice")
        self.assertEqual(connection.execute("SELECT deleted_at FROM reimbursements").fetchone()[0], "deleted")
        backup.verify_backup(archive)

    def test_does_not_replace_existing_archive(self):
        backup = self.module()
        first = backup.create_backup(self.data, self.destination, now=self.now)
        original = first.read_bytes()
        with self.assertRaises(FileExistsError):
            backup.create_backup(self.data, self.destination, now=self.now)
        self.assertEqual(first.read_bytes(), original)

    def test_missing_required_inputs_and_claimed_rows_fail_cleanly(self):
        backup = self.module()
        relative = self.add_record(deleted=True)
        with self.database.transaction() as connection:
            connection.execute("UPDATE reimbursements SET purge_claim = ?", ("a" * 64,))
        with self.assertRaisesRegex(ValueError, "purge_claim"):
            backup.create_backup(self.data, self.destination)
        with self.database.transaction() as connection:
            connection.execute("UPDATE reimbursements SET purge_claim = NULL")
        for required in (self.data / relative / "claim.pdf", self.data / "app-secret", self.database.path):
            original = required.read_bytes()
            required.unlink()
            with self.subTest(required=required), self.assertRaises((ValueError, FileNotFoundError)):
                backup.create_backup(self.data, self.destination)
            required.write_bytes(original)
        self.assertFalse(list(self.destination.glob("*")))

    def test_rejects_owned_symlink_hardlink_and_nonregular_file(self):
        backup = self.module()
        relative = self.add_record()
        artifact = self.data / relative / "claim.pdf"
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"private")
        artifact.unlink()
        artifact.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            backup.create_backup(self.data, self.destination)
        artifact.unlink()
        artifact.hardlink_to(outside)
        with self.assertRaises(ValueError):
            backup.create_backup(self.data, self.destination)
        artifact.unlink()
        artifact.mkdir()
        with self.assertRaises((OSError, ValueError)):
            backup.create_backup(self.data, self.destination)
        self.assertEqual(outside.read_bytes(), b"private")
        self.assertFalse(list(self.destination.glob("*")))

    def test_rejects_paths_outside_owners_record(self):
        backup = self.module()
        relative = self.add_record()
        for path in ("../private.pdf", "/etc/passwd", str(relative / "nested" / ".." / "claim.pdf"),
                     "users/2/10000000-0000-4000-8000-000000000001/claim.pdf"):
            with self.database.transaction() as connection:
                connection.execute("UPDATE reimbursements SET pdf_path = ?", (path,))
            with self.subTest(path=path), self.assertRaises(ValueError):
                backup.create_backup(self.data, self.destination)

    def test_rejects_overlapping_broad_and_symlink_roots(self):
        backup = self.module()
        alias = self.root / "alias"
        alias.symlink_to(self.data, target_is_directory=True)
        for data, destination in ((self.data, self.data / "backups"), (self.data, self.root),
                                  (self.data, Path("/")), (alias, self.destination)):
            with self.subTest(data=data, destination=destination), self.assertRaises(ValueError):
                backup.create_backup(data, destination)
        alias.unlink()
        self.destination.mkdir(exist_ok=True)
        alias.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            backup.create_backup(self.data, alias)

    def test_restore_rejects_tampered_manifest_and_unsafe_tar_members(self):
        backup = self.module()
        archive = backup.create_backup(self.data, self.destination, now=self.now)
        with tarfile.open(archive) as tar:
            originals = [(member.name, tar.extractfile(member).read()) for member in tar]
        for extra in ("../escape", "users/link", "app-secret"):
            bad = self.root / "bad.tar.gz"
            with tarfile.open(bad, "w:gz") as tar:
                for name, content in originals:
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                info = tarfile.TarInfo(extra)
                info.size = 3
                tar.addfile(info, io.BytesIO(b"bad"))
            target = self.root / "restored"
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                backup.restore_backup(bad, target)
            self.assertFalse(target.exists())
        self.assertFalse((self.root / "escape").exists())

    def test_restore_never_overwrites_existing_directory(self):
        backup = self.module()
        archive = backup.create_backup(self.data, self.destination, now=self.now)
        with self.assertRaises(FileExistsError):
            backup.restore_backup(archive, self.data)

    def test_failed_snapshot_leaves_no_archive_or_staging_directory(self):
        backup = self.module()
        with mock.patch.object(Database, "backup", side_effect=OSError("snapshot failed")):
            with self.assertRaisesRegex(OSError, "snapshot failed"):
                backup.create_backup(self.data, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_manifest_detects_changed_secret_bytes(self):
        backup = self.module()
        original = backup.create_backup(self.data, self.destination, now=self.now)
        changed = self.root / "changed.tar.gz"
        with tarfile.open(original) as source, tarfile.open(changed, "w:gz") as target:
            for member in source:
                content = source.extractfile(member).read()
                if member.name == "app-secret":
                    content = b"t" * 32
                target.addfile(member, io.BytesIO(content))
        with self.assertRaisesRegex(ValueError, "SHA256"):
            backup.verify_backup(changed)


if __name__ == "__main__":
    unittest.main()
