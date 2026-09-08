from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import json
import os
import tempfile
from pathlib import Path
import threading
import unittest
from unittest import mock

import app
import reimbursements
from config import AppConfig
from database import Database
from reimbursements import ReimbursementNotFound, ReimbursementService


class ReimbursementCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.database = Database(self.data_dir / "database" / "app.db")
        self.database.migrate()
        self.config = AppConfig(
            template_path=self.root / "template.xlsx",
            data_dir=self.data_dir,
            templates_dir=self.root / "templates",
            static_dir=self.root / "static",
            host="127.0.0.1",
            port=0,
            soffice_path="/fake/soffice",
            max_body_bytes=1024,
            max_concurrent_generations=1,
            cookie_secure=False,
        )
        self.service = ReimbursementService(self.database, self.config)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "template_path": "template.xlsx",
                    "data_dir": "server-data",
                    "templates_dir": "templates",
                    "static_dir": "static",
                    "host": "127.0.0.1",
                    "port": 0,
                    "max_body_bytes": 1024,
                }
            ),
            encoding="utf-8",
        )
        self.user_id = 1
        self.other_user_id = 2
        self.record_id = "10000000-0000-4000-8000-000000000001"
        now = "2026-09-08T00:00:00+00:00"
        with self.database.transaction(immediate=True) as connection:
            for user_id, username in (
                (self.user_id, "alice"),
                (self.other_user_id, "bob"),
            ):
                connection.execute(
                    """INSERT INTO users(
                        id, username, username_key, password_hash, password_salt,
                        password_params, real_name, department, role, status,
                        must_change_password, created_at, approved_at, updated_at
                    ) VALUES (?, ?, ?, X'00', X'00', '{}',
                        '张三', '技术部', 'user', 'active', 0, ?, ?, ?)""",
                    (user_id, username, username, now, now, now),
                )
        self._insert_record(self.user_id, self.record_id, created_at=now)

    def tearDown(self):
        self.temporary.cleanup()

    def _insert_record(
        self,
        user_id: int,
        record_id: str,
        *,
        created_at: str,
        deleted_at: str | None = None,
        create_files: bool = False,
    ) -> Path:
        record_dir = self.data_dir / "users" / str(user_id) / record_id
        if create_files:
            record_dir.mkdir(parents=True)
            (record_dir / "claim.xlsx").write_bytes(b"xlsx")
            (record_dir / "claim.pdf").write_bytes(b"pdf")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO reimbursements(
                    id, user_id, reimbursement_date, display_name,
                    xlsx_path, pdf_path, created_at, deleted_at
                ) VALUES (?, ?, '2026-09-08', 'claim.xlsx', ?, ?, ?, ?)""",
                (
                    record_id,
                    user_id,
                    f"users/{user_id}/{record_id}/claim.xlsx",
                    f"users/{user_id}/{record_id}/claim.pdf",
                    created_at,
                    deleted_at,
                ),
            )
        return record_dir

    def _row(self, record_id: str):
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT * FROM reimbursements WHERE id = ?", (record_id,)
            ).fetchone()

    def _create_partial_purge(self, service: ReimbursementService) -> Path:
        owner_dir = self.data_dir / "users" / str(self.user_id)
        record_dir = owner_dir / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "original-sentinel").write_bytes(b"original")
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        service.trash(self.user_id, self.record_id)

        with mock.patch.object(
            service,
            "_remove_tree",
            side_effect=OSError("forced partial purge"),
        ), self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(ReimbursementNotFound):
                service.purge_one(self.user_id, self.record_id)

        quarantines = list(
            owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
        )
        self.assertEqual(len(quarantines), 1)
        return quarantines[0]

    def _create_empty_purge_quarantine(
        self,
        service: ReimbursementService,
    ) -> Path:
        owner_dir = self.data_dir / "users" / str(self.user_id)
        record_dir = owner_dir / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        service.trash(self.user_id, self.record_id)
        real_rmdir = reimbursements.os.rmdir
        failed_outer_rmdir = False

        def fail_outer_quarantine_once(path, *args, **kwargs):
            nonlocal failed_outer_rmdir
            if (
                not failed_outer_rmdir
                and isinstance(path, str)
                and path.startswith(f".reimbursement-purge-{self.record_id}-")
            ):
                failed_outer_rmdir = True
                raise OSError("forced final quarantine rmdir failure")
            return real_rmdir(path, *args, **kwargs)

        with mock.patch(
            "reimbursements.os.rmdir",
            side_effect=fail_outer_quarantine_once,
        ), self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(ReimbursementNotFound):
                service.purge_one(self.user_id, self.record_id)

        quarantines = list(
            owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
        )
        self.assertTrue(failed_outer_rmdir)
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(list(quarantines[0].iterdir()), [])
        self.assertIsNotNone(self._row(self.record_id))
        return quarantines[0]

    def test_owner_can_trash_active_record_at_supplied_time(self):
        deleted_at = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
        trash = getattr(self.service, "trash", None)
        self.assertIsNotNone(trash, "ReimbursementService.trash must exist")

        record = trash(self.user_id, self.record_id, now=deleted_at)

        self.assertEqual(record.id, self.record_id)
        self.assertEqual(record.deleted_at, deleted_at.isoformat())
        self.assertEqual(self.service.list_active(self.user_id), [])
        self.assertEqual([item.id for item in self.service.list_trash(self.user_id)], [self.record_id])

    def test_restore_is_owner_scoped_and_requires_a_trashed_record(self):
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        deleted_at = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
        self.service.trash(self.user_id, self.record_id, now=deleted_at)

        with self.assertRaisesRegex(ReimbursementNotFound, "^报销记录不存在$"):
            self.service.restore(self.other_user_id, self.record_id)
        restored = self.service.restore(self.user_id, self.record_id)

        self.assertIsNone(restored.deleted_at)
        with self.assertRaisesRegex(ReimbursementNotFound, "^报销记录不存在$"):
            self.service.restore(self.user_id, self.record_id)

    def test_purge_one_removes_only_an_owned_trashed_record(self):
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        self.service.trash(
            self.user_id,
            self.record_id,
            now=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

        with self.assertRaisesRegex(ReimbursementNotFound, "^报销记录不存在$"):
            self.service.purge_one(self.other_user_id, self.record_id)
        self.service.purge_one(self.user_id, self.record_id)

        self.assertFalse(record_dir.exists())
        self.assertIsNone(self._row(self.record_id))

    def test_purge_one_rejects_active_record_without_touching_files(self):
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        sentinel = record_dir / "claim.xlsx"
        sentinel.write_bytes(b"keep")

        with self.assertRaisesRegex(ReimbursementNotFound, "^报销记录不存在$"):
            self.service.purge_one(self.user_id, self.record_id)

        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertIsNotNone(self._row(self.record_id))

    def test_filesystem_removal_failure_retains_database_row_for_retry(self):
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        self.service.trash(self.user_id, self.record_id)

        def fail_removal(_name, **kwargs):
            reimbursements.os.unlink("claim.xlsx", dir_fd=kwargs["root_fd"])
            raise OSError("private filesystem detail")

        service = ReimbursementService(
            self.database,
            self.config,
            remove_tree=fail_removal,
        )
        with self.assertLogs("reimbursements", level="ERROR") as captured:
            with self.assertRaises(ReimbursementNotFound):
                service.purge_one(self.user_id, self.record_id)

        self.assertIsNotNone(self._row(self.record_id))
        self.assertFalse(record_dir.exists())
        owner_dir = self.data_dir / "users" / str(self.user_id)
        quarantines = list(
            owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
        )
        self.assertEqual(len(quarantines), 1)
        isolated = quarantines[0] / "entry"
        self.assertFalse((isolated / "claim.xlsx").exists())
        self.assertEqual((isolated / "claim.pdf").read_bytes(), b"pdf")
        self.assertNotIn("private filesystem detail", "\n".join(captured.output))

    def test_partial_unlink_failure_keeps_record_reachable_for_retry(self):
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        self.service.trash(self.user_id, self.record_id)
        real_unlink = reimbursements.os.unlink
        unlink_count = 0

        def fail_second_entry_once(path, *args, **kwargs):
            nonlocal unlink_count
            if path == "entry":
                unlink_count += 1
            if path == "entry" and unlink_count == 2:
                raise OSError("forced partial unlink failure")
            return real_unlink(path, *args, **kwargs)

        with self.assertLogs("reimbursements", level="ERROR"), mock.patch(
            "reimbursements.os.unlink", side_effect=fail_second_entry_once
        ):
            with self.assertRaises(ReimbursementNotFound):
                self.service.purge_one(self.user_id, self.record_id)

        self.assertEqual(unlink_count, 2)
        self.assertIsNotNone(self._row(self.record_id))

        self.service.purge_one(self.user_id, self.record_id)

        self.assertIsNone(self._row(self.record_id))
        owner_dir = self.data_dir / "users" / str(self.user_id)
        self.assertEqual(list(owner_dir.iterdir()), [])

    def test_purge_retains_row_when_record_disappears_after_inode_capture(self):
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        sentinel = record_dir / "claim.xlsx"
        sentinel.write_bytes(b"owned")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        detached = self.root / "detached-record"
        record_identity = self.service._directory_identity(record_dir.stat())
        real_identity = self.service._directory_identity
        identity_checks = 0
        self.service.trash(self.user_id, self.record_id)

        def detach_after_first_identity(metadata):
            nonlocal identity_checks
            identity = real_identity(metadata)
            if identity == record_identity:
                identity_checks += 1
                if identity_checks == 1:
                    record_dir.rename(detached)
            return identity

        with mock.patch.object(
            self.service,
            "_directory_identity",
            side_effect=detach_after_first_identity,
        ):
            with self.assertRaises(ReimbursementNotFound):
                self.service.purge_one(self.user_id, self.record_id)

        self.assertEqual(identity_checks, 1)
        self.assertEqual((detached / sentinel.name).read_bytes(), b"owned")
        self.assertIsNotNone(self._row(self.record_id))

    def test_purge_isolates_before_deleting_record_replaced_after_reopen(self):
        owner_dir = self.data_dir / "users" / str(self.user_id)
        record_dir = owner_dir / self.record_id
        record_dir.mkdir(parents=True)
        owned_sentinel = record_dir / "owned-sentinel"
        owned_sentinel.write_bytes(b"owned")
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        detached = self.root / "detached-owned-record"
        replacement = self.root / "replacement-record"
        replacement.mkdir()
        replacement_sentinel = replacement / "replacement-sentinel"
        replacement_sentinel.write_bytes(b"replacement")
        record_identity = self.service._directory_identity(record_dir.stat())
        real_identity = self.service._directory_identity
        identity_checks = 0
        self.service.trash(self.user_id, self.record_id)

        def replace_after_second_identity(metadata):
            nonlocal identity_checks
            identity = real_identity(metadata)
            if identity == record_identity:
                identity_checks += 1
                if identity_checks == 2:
                    record_dir.rename(detached)
                    replacement.rename(record_dir)
            return identity

        with mock.patch.object(
            self.service,
            "_directory_identity",
            side_effect=replace_after_second_identity,
        ):
            with self.assertRaises(ReimbursementNotFound):
                self.service.purge_one(self.user_id, self.record_id)

        self.assertEqual(identity_checks, 2)
        self.assertTrue((detached / owned_sentinel.name).exists())
        self.assertEqual((detached / owned_sentinel.name).read_bytes(), b"owned")
        replacement_matches = list(owner_dir.rglob(replacement_sentinel.name))
        self.assertEqual(len(replacement_matches), 1)
        self.assertEqual(replacement_matches[0].read_bytes(), b"replacement")
        self.assertIsNotNone(self._row(self.record_id))

    def test_purge_retry_rejects_replaced_quarantine_entry(self):
        owner_dir = self.data_dir / "users" / str(self.user_id)
        record_dir = owner_dir / self.record_id
        record_dir.mkdir(parents=True)
        original_sentinel = record_dir / "original-sentinel"
        original_sentinel.write_bytes(b"original")
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        device, inode = self.service._directory_identity(record_dir.stat())
        quarantine = owner_dir / (
            f".reimbursement-purge-{self.record_id}-{device:x}-{inode:x}"
        )
        quarantine.mkdir()
        isolated = quarantine / "entry"
        record_dir.rename(isolated)
        detached = self.root / "detached-isolated-record"
        isolated.rename(detached)
        isolated.mkdir()
        replacement_sentinel = isolated / "replacement-sentinel"
        replacement_sentinel.write_bytes(b"replacement")
        self.service.trash(self.user_id, self.record_id)

        with self.assertRaises(ReimbursementNotFound):
            self.service.purge_one(self.user_id, self.record_id)

        self.assertEqual((detached / original_sentinel.name).read_bytes(), b"original")
        self.assertEqual(replacement_sentinel.read_bytes(), b"replacement")
        self.assertIsNotNone(self._row(self.record_id))

    def test_purge_retry_rejects_self_consistent_forged_quarantine(self):
        owner_dir = self.data_dir / "users" / str(self.user_id)
        record_dir = owner_dir / self.record_id
        record_dir.mkdir(parents=True)
        original_sentinel = record_dir / "original-sentinel"
        original_sentinel.write_bytes(b"original")
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        self.service.trash(self.user_id, self.record_id)

        with mock.patch.object(
            self.service,
            "_remove_tree",
            side_effect=OSError("forced partial purge"),
        ), self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(ReimbursementNotFound):
                self.service.purge_one(self.user_id, self.record_id)

        legitimate = list(
            owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
        )
        self.assertEqual(len(legitimate), 1)
        detached_legitimate = self.root / "detached-legitimate-quarantine"
        legitimate[0].rename(detached_legitimate)

        forged_staging = owner_dir / "forged-quarantine"
        replacement = forged_staging / "entry"
        replacement.mkdir(parents=True)
        replacement_sentinel = replacement / "replacement-sentinel"
        replacement_sentinel.write_bytes(b"replacement")
        device, inode = self.service._directory_identity(replacement.stat())
        forged = owner_dir / (
            f".reimbursement-purge-{self.record_id}-{device:x}-{inode:x}"
        )
        forged_staging.rename(forged)

        with self.assertRaises(ReimbursementNotFound):
            self.service.purge_one(self.user_id, self.record_id)

        self.assertEqual(
            (detached_legitimate / "entry" / original_sentinel.name).read_bytes(),
            b"original",
        )
        self.assertEqual(
            (forged / "entry" / replacement_sentinel.name).read_bytes(),
            b"replacement",
        )
        self.assertIsNotNone(self._row(self.record_id))

    def test_purge_retry_with_same_secret_resumes_after_service_restart(self):
        app_secret = b"a" * 32
        first_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        quarantine = self._create_partial_purge(first_service)

        restarted_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        restarted_service.purge_one(self.user_id, self.record_id)

        self.assertFalse(quarantine.exists())
        self.assertIsNone(self._row(self.record_id))

    def test_fresh_purge_retry_completes_empty_authenticated_quarantine(self):
        app_secret = b"a" * 32
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        quarantine = self._create_empty_purge_quarantine(service)

        restarted_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        try:
            restarted_service.purge_one(self.user_id, self.record_id)
        except ReimbursementNotFound:
            self.fail("same-secret retry must complete the empty quarantine")

        self.assertFalse(quarantine.exists())
        self.assertIsNone(self._row(self.record_id))

    def test_resumed_purge_retry_completes_empty_authenticated_quarantine(self):
        app_secret = b"a" * 32
        first_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        quarantine = self._create_partial_purge(first_service)
        resumed_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        real_rmdir = reimbursements.os.rmdir
        failed_outer_rmdir = False

        def fail_outer_quarantine_once(path, *args, **kwargs):
            nonlocal failed_outer_rmdir
            if path == quarantine.name and not failed_outer_rmdir:
                failed_outer_rmdir = True
                raise OSError("forced resumed quarantine rmdir failure")
            return real_rmdir(path, *args, **kwargs)

        with mock.patch(
            "reimbursements.os.rmdir",
            side_effect=fail_outer_quarantine_once,
        ), self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(ReimbursementNotFound):
                resumed_service.purge_one(self.user_id, self.record_id)

        self.assertTrue(failed_outer_rmdir)
        self.assertEqual(list(quarantine.iterdir()), [])
        self.assertIsNotNone(self._row(self.record_id))

        restarted_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        try:
            restarted_service.purge_one(self.user_id, self.record_id)
        except ReimbursementNotFound:
            self.fail("same-secret retry must complete the empty quarantine")

        self.assertFalse(quarantine.exists())
        self.assertIsNone(self._row(self.record_id))

    def test_empty_quarantine_with_different_secret_is_retained(self):
        first_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        quarantine = self._create_empty_purge_quarantine(first_service)
        different_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"b" * 32,
        )

        with self.assertRaises(ReimbursementNotFound):
            different_service.purge_one(self.user_id, self.record_id)

        self.assertEqual(list(quarantine.iterdir()), [])
        self.assertIsNotNone(self._row(self.record_id))

    def test_empty_quarantine_with_fresh_default_key_is_retained(self):
        with mock.patch(
            "reimbursements.os.urandom",
            side_effect=(b"a" * 32, b"b" * 32),
        ):
            first_service = ReimbursementService(self.database, self.config)
            restarted_service = ReimbursementService(self.database, self.config)
        quarantine = self._create_empty_purge_quarantine(first_service)

        with self.assertRaises(ReimbursementNotFound):
            restarted_service.purge_one(self.user_id, self.record_id)

        self.assertEqual(list(quarantine.iterdir()), [])
        self.assertIsNotNone(self._row(self.record_id))

    def test_empty_quarantine_with_forged_signed_field_is_retained(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        service.trash(self.user_id, self.record_id)
        owner_dir = self.data_dir / "users" / str(self.user_id)
        owner_dir.mkdir(parents=True)
        valid_name = service._purge_quarantine_name(self.record_id, (1, 2))
        prefix = f".reimbursement-purge-{self.record_id}-"
        device, inode, mac = valid_name[len(prefix):].split("-")
        forged = owner_dir / f"{prefix}{int(device, 16) + 1:x}-{inode}-{mac}"
        forged.mkdir()

        with self.assertRaises(ReimbursementNotFound):
            service.purge_one(self.user_id, self.record_id)

        self.assertEqual(list(forged.iterdir()), [])
        self.assertIsNotNone(self._row(self.record_id))

    def test_authenticated_empty_quarantine_with_unknown_content_is_retained(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        quarantine = self._create_empty_purge_quarantine(service)
        unknown = quarantine / "unknown"
        unknown.write_bytes(b"keep")

        with self.assertRaises(ReimbursementNotFound):
            service.purge_one(self.user_id, self.record_id)

        self.assertEqual(unknown.read_bytes(), b"keep")
        self.assertIsNotNone(self._row(self.record_id))

    def test_empty_quarantine_identity_race_retains_replacement(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        quarantine = self._create_empty_purge_quarantine(service)
        original_metadata = quarantine.stat()
        detached = self.root / "detached-empty-quarantine"
        replacement = self.root / "replacement-empty-quarantine"
        replacement.mkdir()
        replacement_metadata = replacement.stat()
        real_stat = reimbursements.os.stat
        quarantine_stats = 0

        def replace_after_final_identity_check(path, *args, **kwargs):
            nonlocal quarantine_stats
            metadata = real_stat(path, *args, **kwargs)
            if path == quarantine.name:
                quarantine_stats += 1
                if quarantine_stats == 2:
                    quarantine.rename(detached)
                    replacement.rename(quarantine)
            return metadata

        with mock.patch(
            "reimbursements.os.stat",
            side_effect=replace_after_final_identity_check,
        ):
            with self.assertRaises(ReimbursementNotFound):
                service.purge_one(self.user_id, self.record_id)

        self.assertEqual(quarantine_stats, 2)
        self.assertTrue(os.path.samestat(original_metadata, detached.stat()))
        retained_metadata = [
            candidate.lstat()
            for candidate in (self.data_dir / "users").rglob("*")
        ]
        self.assertTrue(
            any(
                os.path.samestat(replacement_metadata, metadata)
                for metadata in retained_metadata
            )
        )
        self.assertIsNotNone(self._row(self.record_id))

    def test_wrapper_close_failure_does_not_downgrade_empty_completion(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        quarantine = self._create_empty_purge_quarantine(service)
        real_create = ReimbursementService._create_quarantine_directory
        real_close = reimbursements.os.close
        wrapper_fd = None
        wrapper_close_attempts = 0

        def capture_wrapper(parent_fd):
            nonlocal wrapper_fd
            name, descriptor = real_create(parent_fd)
            wrapper_fd = descriptor
            return name, descriptor

        def fail_wrapper_close(descriptor):
            nonlocal wrapper_close_attempts
            if descriptor == wrapper_fd:
                wrapper_close_attempts += 1
                self.assertEqual(reimbursements.os.listdir(descriptor), [])
                raise OSError("forced persistent wrapper close failure")
            return real_close(descriptor)

        first_failure = None
        try:
            with mock.patch.object(
                ReimbursementService,
                "_create_quarantine_directory",
                side_effect=capture_wrapper,
            ), mock.patch(
                "reimbursements.os.close",
                side_effect=fail_wrapper_close,
            ):
                try:
                    service.purge_one(self.user_id, self.record_id)
                except ReimbursementNotFound as error:
                    first_failure = error
        finally:
            if wrapper_fd is not None:
                real_close(wrapper_fd)

        retry_failed = False
        if first_failure is not None:
            try:
                service.purge_one(self.user_id, self.record_id)
            except ReimbursementNotFound:
                retry_failed = True

        self.assertEqual(
            (
                first_failure is not None,
                retry_failed,
                wrapper_close_attempts,
                self._row(self.record_id) is not None,
                quarantine.exists(),
            ),
            (False, False, 1, False, False),
        )

    def test_purge_retry_with_different_secret_retains_quarantine_and_row(self):
        first_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        quarantine = self._create_partial_purge(first_service)

        restarted_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"b" * 32,
        )
        with self.assertRaises(ReimbursementNotFound):
            restarted_service.purge_one(self.user_id, self.record_id)

        self.assertEqual(
            (quarantine / "entry" / "original-sentinel").read_bytes(),
            b"original",
        )
        self.assertIsNotNone(self._row(self.record_id))

    def test_default_service_keys_do_not_authenticate_cross_service_retry(self):
        with mock.patch(
            "reimbursements.os.urandom",
            side_effect=(b"a" * 32, b"b" * 32),
        ) as random_bytes:
            first_service = ReimbursementService(self.database, self.config)
            quarantine = self._create_partial_purge(first_service)
            restarted_service = ReimbursementService(self.database, self.config)

        with self.assertRaises(ReimbursementNotFound):
            restarted_service.purge_one(self.user_id, self.record_id)

        self.assertEqual(
            random_bytes.call_args_list,
            [mock.call(32), mock.call(32)],
        )
        self.assertEqual(
            (quarantine / "entry" / "original-sentinel").read_bytes(),
            b"original",
        )
        self.assertIsNotNone(self._row(self.record_id))

    def test_purge_quarantine_name_requires_canonical_authenticated_fields(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        identity = (0xABCDEF, 0x123ABC)
        name = service._purge_quarantine_name(self.record_id, identity)
        prefix = f".reimbursement-purge-{self.record_id}-"
        components = name[len(prefix):].split("-")

        self.assertEqual(len(components), 3)
        device, inode, mac = components
        self.assertEqual(
            service._purge_quarantine_identity(name, self.record_id),
            identity,
        )
        invalid_names = (
            f"{prefix}0{device}-{inode}-{mac}",
            f"{prefix}{device.upper()}-{inode}-{mac}",
            f"{prefix}{device}-0{inode}-{mac}",
            f"{prefix}{device}-{inode}-A{mac[1:]}",
            f"{prefix}{device}-{inode}-{'0' if mac[0] != '0' else '1'}{mac[1:]}",
            f"{name}-extra",
        )
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=invalid_name):
                self.assertIsNone(
                    service._purge_quarantine_identity(
                        invalid_name,
                        self.record_id,
                    )
                )

    def test_purge_quarantine_mac_uses_constant_time_comparison(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        identity = (1, 2)
        name = service._purge_quarantine_name(self.record_id, identity)
        real_compare_digest = hmac.compare_digest

        with mock.patch(
            "hmac.compare_digest",
            wraps=real_compare_digest,
        ) as compare_digest:
            parsed = service._purge_quarantine_identity(name, self.record_id)

        self.assertEqual(parsed, identity)
        compare_digest.assert_called_once()

    def test_restore_rejects_record_retained_after_partial_purge(self):
        owner_dir = self.data_dir / "users" / str(self.user_id)
        record_dir = owner_dir / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        self.service.trash(self.user_id, self.record_id)

        def fail_after_isolation(_name, **_kwargs):
            raise OSError("forced partial purge")

        failing_service = ReimbursementService(
            self.database,
            self.config,
            remove_tree=fail_after_isolation,
        )
        with self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(ReimbursementNotFound):
                failing_service.purge_one(self.user_id, self.record_id)

        with self.assertRaises(ReimbursementNotFound):
            self.service.restore(self.user_id, self.record_id)

        row = self._row(self.record_id)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(self.service.list_active(self.user_id), [])
        self.assertEqual(
            [record.id for record in self.service.list_trash(self.user_id)],
            [self.record_id],
        )
        self.assertFalse(record_dir.exists())
        self.assertEqual(
            len(list(owner_dir.glob(f".reimbursement-purge-{self.record_id}-*"))),
            1,
        )

    def test_purge_rejects_paths_outside_exact_owner_record_directory(self):
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        own_file = record_dir / "claim.xlsx"
        own_file.write_bytes(b"own")
        other_id = "10000000-0000-4000-8000-000000000099"
        other_dir = self.data_dir / "users" / str(self.other_user_id) / other_id
        other_dir.mkdir(parents=True)
        external = other_dir / "claim.xlsx"
        external.write_bytes(b"other")
        self.service.trash(self.user_id, self.record_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE reimbursements SET xlsx_path = ?
                WHERE id = ? AND user_id = ?""",
                (
                    f"users/{self.other_user_id}/{other_id}/claim.xlsx",
                    self.record_id,
                    self.user_id,
                ),
            )

        with self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(ReimbursementNotFound):
                self.service.purge_one(self.user_id, self.record_id)

        self.assertEqual(own_file.read_bytes(), b"own")
        self.assertEqual(external.read_bytes(), b"other")
        self.assertIsNotNone(self._row(self.record_id))

    def test_expired_purge_continues_and_ignores_active_recent_and_orphan_entries(self):
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        expired_at = (now - timedelta(days=31)).isoformat()
        recent_at = (now - timedelta(days=1)).isoformat()
        expired_id = "20000000-0000-4000-8000-000000000001"
        other_expired_id = "20000000-0000-4000-8000-000000000002"
        recent_id = "20000000-0000-4000-8000-000000000003"
        failed_id = "20000000-0000-4000-8000-000000000004"
        orphan_id = "20000000-0000-4000-8000-000000000005"
        expired = self._insert_record(
            self.user_id,
            expired_id,
            created_at=expired_at,
            deleted_at=expired_at,
            create_files=True,
        )
        other_expired = self._insert_record(
            self.other_user_id,
            other_expired_id,
            created_at=expired_at,
            deleted_at=expired_at,
            create_files=True,
        )
        recent = self._insert_record(
            self.user_id,
            recent_id,
            created_at=recent_at,
            deleted_at=recent_at,
            create_files=True,
        )
        failed = self._insert_record(
            self.user_id,
            failed_id,
            created_at=expired_at,
            deleted_at=expired_at,
            create_files=True,
        )
        orphan = self.data_dir / "users" / str(self.user_id) / orphan_id
        orphan.mkdir(parents=True)
        (orphan / "unknown").write_bytes(b"keep")

        real_cleanup = self.service._cleanup_at

        def fail_one(parent_fd, name, identities, record_id, **kwargs):
            if record_id == failed_id:
                return False
            return real_cleanup(
                parent_fd,
                name,
                identities,
                record_id,
                **kwargs,
            )

        with mock.patch.object(self.service, "_cleanup_at", side_effect=fail_one):
            purged = self.service.purge_expired(now=now)

        self.assertEqual(purged, 2)
        self.assertFalse(expired.exists())
        self.assertFalse(other_expired.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(failed.exists())
        self.assertEqual((orphan / "unknown").read_bytes(), b"keep")
        self.assertIsNotNone(self._row(self.record_id))
        self.assertIsNotNone(self._row(recent_id))
        self.assertIsNotNone(self._row(failed_id))

    def test_expired_purge_rechecks_cutoff_after_restore_and_retrash(self):
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        expired_at = now - timedelta(days=31)
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        self.service.trash(self.user_id, self.record_id, now=expired_at)
        real_connect = self.database.connect
        raced = False

        class RacingCursor:
            def __init__(cursor_self, cursor):
                cursor_self.cursor = cursor

            def fetchall(cursor_self):
                nonlocal raced
                rows = cursor_self.cursor.fetchall()
                self.service.restore(self.user_id, self.record_id)
                self.service.trash(self.user_id, self.record_id, now=now)
                raced = True
                return rows

        class RacingConnection:
            def __init__(connection_self, connection):
                connection_self.connection = connection

            def execute(connection_self, statement, parameters=()):
                cursor = connection_self.connection.execute(statement, parameters)
                if "ORDER BY deleted_at, id" in statement:
                    return RacingCursor(cursor)
                return cursor

            def close(connection_self):
                connection_self.connection.close()

        first_connection = True

        def connect_with_race():
            nonlocal first_connection
            connection = real_connect()
            if first_connection:
                first_connection = False
                return RacingConnection(connection)
            return connection

        with mock.patch.object(self.database, "connect", side_effect=connect_with_race):
            purged = self.service.purge_expired(now=now)

        self.assertTrue(raced)
        self.assertEqual(purged, 0)
        row = self._row(self.record_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["deleted_at"], now.isoformat())
        self.assertTrue(record_dir.exists())

    def test_server_runs_both_purges_once_before_starting_cleanup_runner(self):
        runner_calls = []

        def deterministic_runner(stop_event, cleanup):
            runner_calls.append((stop_event, cleanup))

        with mock.patch.object(
            app.SessionService, "purge_expired", autospec=True, return_value=0
        ) as purge_sessions, mock.patch.object(
            app.ReimbursementService, "purge_expired", autospec=True, return_value=0
        ) as purge_reimbursements:
            server = app.create_server(
                self.config_path,
                cleanup_runner=deterministic_runner,
            )
            try:
                server.cleanup_thread.join(timeout=1)
            finally:
                server.server_close()

        purge_sessions.assert_called_once_with(server.application.session_service)
        purge_reimbursements.assert_called_once_with(server.reimbursement_service)
        self.assertEqual(len(runner_calls), 1)
        self.assertIs(runner_calls[0][0], server.cleanup_stop_event)
        self.assertTrue(server.cleanup_thread.daemon)

    def test_server_close_signals_and_joins_injected_cleanup_runner(self):
        started = threading.Event()
        stopped = threading.Event()

        def deterministic_runner(stop_event, _cleanup):
            started.set()
            stop_event.wait()
            stopped.set()

        server = app.create_server(
            self.config_path,
            cleanup_runner=deterministic_runner,
        )
        self.assertTrue(started.wait(timeout=1))

        server.server_close()

        self.assertTrue(server.cleanup_stop_event.is_set())
        self.assertTrue(stopped.is_set())
        self.assertFalse(server.cleanup_thread.is_alive())

    def test_default_cleanup_loop_uses_one_day_event_wait_without_sleeping(self):
        class DeterministicEvent:
            def __init__(self):
                self.timeouts = []

            def wait(self, timeout):
                self.timeouts.append(timeout)
                return len(self.timeouts) == 2

        event = DeterministicEvent()
        cleanup = mock.Mock()

        app._cleanup_loop(event, cleanup)

        self.assertEqual(event.timeouts, [86400, 86400])
        cleanup.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
