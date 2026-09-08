from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hmac
import inspect
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import threading
import time
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

    def _record_directory_with_files(self) -> Path:
        record_dir = self.data_dir / "users" / str(self.user_id) / self.record_id
        record_dir.mkdir(parents=True)
        (record_dir / "claim.xlsx").write_bytes(b"xlsx")
        (record_dir / "claim.pdf").write_bytes(b"pdf")
        return record_dir

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
            with self.assertRaises(reimbursements.ReimbursementStorageError):
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
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                f"""CREATE TRIGGER fail_empty_marker_delete
                BEFORE DELETE ON reimbursements
                WHEN OLD.id = '{self.record_id}'
                BEGIN
                    SELECT RAISE(ABORT, 'retain empty marker');
                END"""
            )
        try:
            with self.assertRaises(reimbursements.ReimbursementStorageError):
                service.purge_one(self.user_id, self.record_id)
        finally:
            with self.database.transaction(immediate=True) as connection:
                connection.execute("DROP TRIGGER fail_empty_marker_delete")

        quarantines = list(
            owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
        )
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(list(quarantines[0].iterdir()), [])
        self.assertIsNotNone(self._row(self.record_id))
        return quarantines[0]

    def _assert_claimed_empty_marker(self, record_id: str):
        row = self._row(record_id)
        self.assertIsNotNone(row)
        claim = row["purge_claim"]
        self.assertIsInstance(claim, str)
        self.assertRegex(claim, "^[0-9a-f]{64}$")
        owner_dir = self.data_dir / "users" / str(row["user_id"])
        quarantines = list(
            owner_dir.glob(f".reimbursement-purge-{record_id}-*")
        )
        self.assertEqual(len(quarantines), 1)
        self.assertIn(claim, quarantines[0].name)
        self.assertIn(f"-complete-{claim}-", quarantines[0].name)
        self.assertEqual(list(quarantines[0].iterdir()), [])
        return row, quarantines[0]

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

    def test_crash_before_delete_transaction_is_restartable_from_empty_marker(self):
        app_secret = b"a" * 32
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        record_dir = self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        real_transaction = self.database.transaction
        transaction_count = 0

        @contextmanager
        def crash_before_second_transaction(*, immediate=False):
            nonlocal transaction_count
            transaction_count += 1
            if transaction_count == 2:
                raise SystemExit("simulated process crash")
            with real_transaction(immediate=immediate) as connection:
                yield connection

        with mock.patch.object(
            self.database,
            "transaction",
            new=crash_before_second_transaction,
        ), self.assertRaisesRegex(SystemExit, "simulated process crash"):
            service.purge_one(self.user_id, self.record_id)

        _row, marker = self._assert_claimed_empty_marker(self.record_id)
        self.assertFalse(record_dir.exists())
        with self.assertRaises(ReimbursementNotFound):
            service.restore(self.user_id, self.record_id)

        restarted = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        restarted.purge_one(self.user_id, self.record_id)

        self.assertIsNone(self._row(self.record_id))
        self.assertFalse(marker.exists())

    def test_retry_does_not_treat_pre_isolation_empty_marker_as_complete(self):
        app_secret = b"a" * 32
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        record_dir = self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        real_open = ReimbursementService._open_directory
        failed_marker_open = False

        def fail_first_marker_open(parent_fd, name):
            nonlocal failed_marker_open
            if (
                not failed_marker_open
                and name.startswith(f".reimbursement-purge-{self.record_id}-")
            ):
                failed_marker_open = True
                raise OSError("simulated crash after pending marker creation")
            return real_open(parent_fd, name)

        with mock.patch.object(
            ReimbursementService,
            "_open_directory",
            side_effect=fail_first_marker_open,
        ), self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(reimbursements.ReimbursementStorageError):
                service.purge_one(self.user_id, self.record_id)

        row = self._row(self.record_id)
        self.assertIsNotNone(row)
        self.assertRegex(row["purge_claim"], "^[0-9a-f]{64}$")
        owner_dir = record_dir.parent
        markers = list(
            owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
        )
        self.assertTrue(failed_marker_open)
        self.assertEqual(len(markers), 1)
        self.assertEqual(list(markers[0].iterdir()), [])
        self.assertEqual((record_dir / "claim.xlsx").read_bytes(), b"xlsx")
        self.assertEqual((record_dir / "claim.pdf").read_bytes(), b"pdf")

        restarted = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        restarted.purge_one(self.user_id, self.record_id)

        self.assertIsNone(self._row(self.record_id))
        self.assertFalse(record_dir.exists())
        self.assertFalse(markers[0].exists())

    def test_completion_transition_rejects_replaced_marker_identity(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        record_dir = self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        owner_dir = record_dir.parent
        detached = self.root / "detached-completion-marker"
        replacement = self.root / "replacement-completion-marker"
        replacement.mkdir()
        replacement_metadata = replacement.stat()
        real_stat = reimbursements.os.stat
        completion_stats = 0
        completion_path = None

        def replace_before_completion_identity_check(path, *args, **kwargs):
            nonlocal completion_stats, completion_path
            if (
                isinstance(path, str)
                and path.startswith(f".reimbursement-purge-{self.record_id}-")
                and "-complete-" in path
            ):
                completion_stats += 1
                if completion_stats == 2:
                    completion_path = owner_dir / path
                    completion_path.rename(detached)
                    replacement.rename(completion_path)
            return real_stat(path, *args, **kwargs)

        with mock.patch(
            "reimbursements.os.stat",
            side_effect=replace_before_completion_identity_check,
        ):
            with self.assertRaises(reimbursements.ReimbursementStorageError):
                service.purge_one(self.user_id, self.record_id)

        self.assertEqual(completion_stats, 2)
        self.assertIsNotNone(completion_path)
        self.assertFalse(record_dir.exists())
        self.assertIsNotNone(self._row(self.record_id))
        self.assertEqual((detached / "entry" / "claim.xlsx").read_bytes(), b"xlsx")
        self.assertEqual((detached / "entry" / "claim.pdf").read_bytes(), b"pdf")
        self.assertTrue(
            os.path.samestat(replacement_metadata, completion_path.stat())
        )

        with self.assertRaises(reimbursements.ReimbursementStorageError):
            service.purge_one(self.user_id, self.record_id)

        self.assertIsNotNone(self._row(self.record_id))
        self.assertTrue(
            os.path.samestat(replacement_metadata, completion_path.stat())
        )

    def test_retry_rejects_replaced_empty_pending_when_canonical_disappears(self):
        app_secret = b"a" * 32
        first_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        record_dir = self._record_directory_with_files()
        first_service.trash(self.user_id, self.record_id)
        with mock.patch.object(
            first_service,
            "_resume_purge_at",
            return_value=None,
        ):
            with self.assertRaises(reimbursements.ReimbursementStorageError):
                first_service.purge_one(self.user_id, self.record_id)
        owner_dir = record_dir.parent
        pending_markers = list(
            owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
        )
        self.assertEqual(len(pending_markers), 1)
        pending_marker = pending_markers[0]
        self.assertNotIn("-complete-", pending_marker.name)
        self.assertEqual(list(pending_marker.iterdir()), [])
        detached_marker = self.root / "detached-pending-marker"
        detached_record = self.root / "detached-pending-record"
        replacement = self.root / "replacement-pending-marker"
        replacement.mkdir()
        replacement_metadata = replacement.stat()
        pending_marker.rename(detached_marker)
        replacement.rename(pending_marker)
        record_dir.rename(detached_record)

        restarted_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        with self.assertRaises(reimbursements.ReimbursementStorageError):
            restarted_service.purge_one(self.user_id, self.record_id)

        self.assertIsNotNone(self._row(self.record_id))
        self.assertEqual(list(detached_marker.iterdir()), [])
        self.assertEqual((detached_record / "claim.xlsx").read_bytes(), b"xlsx")
        self.assertEqual((detached_record / "claim.pdf").read_bytes(), b"pdf")
        self.assertTrue(pending_marker.exists())
        self.assertTrue(
            os.path.samestat(replacement_metadata, pending_marker.stat())
        )

    def test_delete_abort_retains_claim_and_empty_marker_for_restart(self):
        app_secret = b"a" * 32
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        record_dir = self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                f"""CREATE TRIGGER fail_reimbursement_delete
                BEFORE DELETE ON reimbursements
                WHEN OLD.id = '{self.record_id}'
                BEGIN
                    SELECT RAISE(ABORT, 'forced delete abort');
                END"""
            )

        with self.assertRaisesRegex(
            reimbursements.ReimbursementStorageError,
            "^报销记录暂时无法删除，请稍后重试$",
        ):
            service.purge_one(self.user_id, self.record_id)

        _row, marker = self._assert_claimed_empty_marker(self.record_id)
        self.assertFalse(record_dir.exists())
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DROP TRIGGER fail_reimbursement_delete")

        restarted = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        restarted.purge_one(self.user_id, self.record_id)

        self.assertIsNone(self._row(self.record_id))
        self.assertFalse(marker.exists())

    def test_delete_commit_failure_retains_claim_and_empty_marker_for_retry(self):
        app_secret = b"a" * 32
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        record_dir = self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        transaction_count = 0

        @contextmanager
        def fail_second_commit(*, immediate=False):
            nonlocal transaction_count
            transaction_count += 1
            connection = self.database.connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                if transaction_count == 2:
                    connection.rollback()
                    raise sqlite3.OperationalError("forced delete commit failure")
                connection.commit()
            finally:
                connection.close()

        with mock.patch.object(
            self.database,
            "transaction",
            new=fail_second_commit,
        ), self.assertRaises(reimbursements.ReimbursementStorageError):
            service.purge_one(self.user_id, self.record_id)

        _row, marker = self._assert_claimed_empty_marker(self.record_id)
        self.assertFalse(record_dir.exists())

        service.purge_one(self.user_id, self.record_id)

        self.assertIsNone(self._row(self.record_id))
        self.assertFalse(marker.exists())

    def test_recursive_purge_does_not_hold_sqlite_writer_lock(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        removal_started = threading.Event()
        allow_removal = threading.Event()
        writer_done = threading.Event()
        purge_errors = []
        writer_errors = []

        def block_recursive_removal(_name, **_kwargs):
            removal_started.set()
            if not allow_removal.wait(timeout=2):
                raise TimeoutError("test did not release recursive removal")

        service._remove_tree = block_recursive_removal

        def purge():
            try:
                service.purge_one(self.user_id, self.record_id)
            except BaseException as error:
                purge_errors.append(error)

        def write_unrelated_setting():
            try:
                with self.database.transaction(immediate=True) as connection:
                    connection.execute(
                        "INSERT INTO app_settings(key, value) VALUES ('during_purge', 'ok')"
                    )
            except BaseException as error:
                writer_errors.append(error)
            finally:
                writer_done.set()

        purge_thread = threading.Thread(target=purge)
        writer_thread = threading.Thread(target=write_unrelated_setting)
        purge_thread.start()
        self.assertTrue(removal_started.wait(timeout=1))
        writer_thread.start()
        try:
            self.assertTrue(
                writer_done.wait(timeout=0.25),
                "recursive filesystem deletion held the SQLite writer lock",
            )
        finally:
            allow_removal.set()
            purge_thread.join(timeout=2)
            writer_thread.join(timeout=2)

        self.assertFalse(purge_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        self.assertEqual(purge_errors, [])
        self.assertEqual(writer_errors, [])

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
        storage_error = getattr(reimbursements, "ReimbursementStorageError", None)
        self.assertIsNotNone(storage_error)
        with self.assertLogs("reimbursements", level="ERROR") as captured:
            with self.assertRaisesRegex(
                storage_error,
                "^报销记录暂时无法删除，请稍后重试$",
            ):
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
            with self.assertRaises(reimbursements.ReimbursementStorageError):
                self.service.purge_one(self.user_id, self.record_id)

        self.assertEqual(unlink_count, 2)
        self.assertIsNotNone(self._row(self.record_id))

        self.service.purge_one(self.user_id, self.record_id)

        self.assertIsNone(self._row(self.record_id))
        owner_dir = self.data_dir / "users" / str(self.user_id)
        tombstones = list(owner_dir.glob(".reimbursement-cleanup-*"))
        self.assertEqual(len(tombstones), 2)
        self.assertTrue(
            all(
                candidate.is_dir()
                for tombstone in tombstones
                for candidate in tombstone.rglob("*")
            )
        )

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
            with self.assertRaises(reimbursements.ReimbursementStorageError):
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
            with self.assertRaises(reimbursements.ReimbursementStorageError):
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

        with self.assertRaises(reimbursements.ReimbursementStorageError):
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
            with self.assertRaises(reimbursements.ReimbursementStorageError):
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

        with self.assertRaises(reimbursements.ReimbursementStorageError):
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
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                f"""CREATE TRIGGER fail_resumed_delete
                BEFORE DELETE ON reimbursements
                WHEN OLD.id = '{self.record_id}'
                BEGIN
                    SELECT RAISE(ABORT, 'retain resumed empty marker');
                END"""
            )
        try:
            with self.assertRaises(reimbursements.ReimbursementStorageError):
                resumed_service.purge_one(self.user_id, self.record_id)
        finally:
            with self.database.transaction(immediate=True) as connection:
                connection.execute("DROP TRIGGER fail_resumed_delete")

        _row, completion_marker = self._assert_claimed_empty_marker(
            self.record_id
        )
        self.assertEqual(completion_marker, quarantine)

        restarted_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        try:
            restarted_service.purge_one(self.user_id, self.record_id)
        except ReimbursementNotFound:
            self.fail("same-secret retry must complete the empty quarantine")

        self.assertFalse(completion_marker.exists())
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

        with self.assertRaises(reimbursements.ReimbursementStorageError):
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

        with self.assertRaises(reimbursements.ReimbursementStorageError):
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
        claim = "c" * 64
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reimbursements SET purge_claim = ? WHERE id = ?",
                (claim, self.record_id),
            )
        owner_dir = self.data_dir / "users" / str(self.user_id)
        owner_dir.mkdir(parents=True)
        valid_name = service._purge_quarantine_name(
            self.record_id,
            claim,
            (1, 2),
        )
        prefix = f".reimbursement-purge-{self.record_id}-"
        encoded_claim, device, inode, mac = valid_name[len(prefix):].split("-")
        forged = owner_dir / (
            f"{prefix}{encoded_claim}-{int(device, 16) + 1:x}-{inode}-{mac}"
        )
        forged.mkdir()

        with self.assertRaises(reimbursements.ReimbursementStorageError):
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

        with self.assertRaises(reimbursements.ReimbursementStorageError):
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
        self.assertIsNone(self._row(self.record_id))

    def test_completion_cleanup_does_not_remove_post_validation_replacement(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        completion_marker = self._create_empty_purge_quarantine(service)
        completion_metadata = completion_marker.stat()
        detached_marker = self.root / "detached-final-completion-marker"
        replacement = self.root / "replacement-final-completion-marker"
        replacement.mkdir()
        replacement_metadata = replacement.stat()
        real_rmdir = reimbursements.os.rmdir
        replaced = False

        def replace_after_final_validation(path, *args, **kwargs):
            nonlocal replaced
            if path == "entry" and not replaced:
                wrapper_fd = kwargs["dir_fd"]
                os.rename(
                    "entry",
                    detached_marker,
                    src_dir_fd=wrapper_fd,
                )
                os.rename(
                    replacement,
                    "entry",
                    dst_dir_fd=wrapper_fd,
                )
                replaced = True
            return real_rmdir(path, *args, **kwargs)

        with mock.patch(
            "reimbursements.os.rmdir",
            side_effect=replace_after_final_validation,
        ):
            service.purge_one(self.user_id, self.record_id)

        self.assertFalse(replaced)
        self.assertFalse(completion_marker.exists())
        self.assertIsNone(self._row(self.record_id))
        retained_metadata = [candidate.lstat() for candidate in self.root.rglob("*")]
        self.assertTrue(
            any(
                os.path.samestat(replacement_metadata, metadata)
                for metadata in retained_metadata
            )
        )
        self.assertTrue(
            any(
                os.path.samestat(completion_metadata, metadata)
                for metadata in retained_metadata
            )
        )

    def test_fresh_purge_does_not_rmdir_validated_record_entry(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        record_dir = self._record_directory_with_files()
        record_metadata = record_dir.stat()
        service.trash(self.user_id, self.record_id)
        detached_record = self.root / "detached-fresh-record-entry"
        replacement = self.root / "replacement-fresh-record-entry"
        replacement.mkdir()
        replacement_metadata = replacement.stat()
        real_rmdir = reimbursements.os.rmdir
        replaced = False

        def replace_before_entry_rmdir(path, *args, **kwargs):
            nonlocal replaced
            if path == "entry" and not replaced:
                marker_fd = kwargs["dir_fd"]
                os.rename("entry", detached_record, src_dir_fd=marker_fd)
                os.rename(replacement, "entry", dst_dir_fd=marker_fd)
                replaced = True
            return real_rmdir(path, *args, **kwargs)

        with mock.patch(
            "reimbursements.os.rmdir",
            side_effect=replace_before_entry_rmdir,
        ):
            service.purge_one(self.user_id, self.record_id)

        self.assertFalse(replaced)
        self.assertIsNone(self._row(self.record_id))
        retained_metadata = [candidate.lstat() for candidate in self.root.rglob("*")]
        self.assertTrue(
            any(
                os.path.samestat(record_metadata, metadata)
                for metadata in retained_metadata
            )
        )
        self.assertTrue(
            any(
                os.path.samestat(replacement_metadata, metadata)
                for metadata in retained_metadata
            )
        )

    def test_complete_retry_does_not_rmdir_validated_record_entry(self):
        app_secret = b"a" * 32
        first_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        completion_marker = self._create_partial_purge(first_service)
        record_metadata = (completion_marker / "entry").stat()
        detached_record = self.root / "detached-retried-record-entry"
        replacement = self.root / "replacement-retried-record-entry"
        replacement.mkdir()
        replacement_metadata = replacement.stat()
        real_rmdir = reimbursements.os.rmdir
        replaced = False

        def replace_before_entry_rmdir(path, *args, **kwargs):
            nonlocal replaced
            if path == "entry" and not replaced:
                marker_fd = kwargs["dir_fd"]
                os.rename("entry", detached_record, src_dir_fd=marker_fd)
                os.rename(replacement, "entry", dst_dir_fd=marker_fd)
                replaced = True
            return real_rmdir(path, *args, **kwargs)

        restarted_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        with mock.patch(
            "reimbursements.os.rmdir",
            side_effect=replace_before_entry_rmdir,
        ):
            restarted_service.purge_one(self.user_id, self.record_id)

        self.assertFalse(replaced)
        self.assertIsNone(self._row(self.record_id))
        retained_metadata = [candidate.lstat() for candidate in self.root.rglob("*")]
        self.assertTrue(
            any(
                os.path.samestat(record_metadata, metadata)
                for metadata in retained_metadata
            )
        )
        self.assertTrue(
            any(
                os.path.samestat(replacement_metadata, metadata)
                for metadata in retained_metadata
            )
        )

    def test_complete_retry_rechecks_canonical_absence_after_cleanup(self):
        app_secret = b"a" * 32
        first_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        completion_marker = self._create_partial_purge(first_service)
        marker_identity = first_service._directory_identity(completion_marker.stat())
        record_dir = completion_marker.parent / self.record_id
        real_listdir = reimbursements.os.listdir
        canonical_created = False

        def create_canonical_after_cleanup(path):
            nonlocal canonical_created
            entries = real_listdir(path)
            if (
                not canonical_created
                and isinstance(path, int)
                and first_service._directory_identity(os.fstat(path))
                == marker_identity
                and not entries
            ):
                record_dir.mkdir()
                (record_dir / "replacement-sentinel").write_bytes(b"replacement")
                canonical_created = True
            return entries

        restarted_service = ReimbursementService(
            self.database,
            self.config,
            app_secret=app_secret,
        )
        with mock.patch(
            "reimbursements.os.listdir",
            side_effect=create_canonical_after_cleanup,
        ):
            with self.assertRaises(reimbursements.ReimbursementStorageError):
                restarted_service.purge_one(self.user_id, self.record_id)

        self.assertTrue(canonical_created)
        self.assertIsNotNone(self._row(self.record_id))
        self.assertEqual(
            (record_dir / "replacement-sentinel").read_bytes(),
            b"replacement",
        )

    def test_successful_purge_retires_data_free_completion_tombstone(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        record_dir = self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)

        service.purge_one(self.user_id, self.record_id)

        owner_dir = record_dir.parent
        tombstones = list(owner_dir.glob(".reimbursement-cleanup-*"))
        self.assertEqual(len(tombstones), 2)
        for tombstone in tombstones:
            self.assertEqual(
                [candidate.name for candidate in tombstone.iterdir()],
                ["entry"],
            )
            self.assertTrue(
                all(candidate.is_dir() for candidate in tombstone.rglob("*"))
            )
        self.assertFalse(record_dir.exists())
        self.assertIsNone(self._row(self.record_id))

    def test_completion_tombstone_does_not_block_reused_record_lifecycle(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        first_record_dir = self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        service.purge_one(self.user_id, self.record_id)
        owner_dir = first_record_dir.parent
        first_tombstones = list(owner_dir.glob(".reimbursement-cleanup-*"))
        self.assertEqual(len(first_tombstones), 2)
        first_tombstone_metadata = [
            tombstone.stat() for tombstone in first_tombstones
        ]
        reused_record_dir = self._insert_record(
            self.user_id,
            self.record_id,
            created_at="2026-09-08T01:00:00+00:00",
            deleted_at="2026-09-08T02:00:00+00:00",
            create_files=True,
        )

        restored = service.restore(self.user_id, self.record_id)
        self.assertIsNone(restored.deleted_at)
        service.trash(self.user_id, self.record_id)
        service.purge_one(self.user_id, self.record_id)

        self.assertFalse(reused_record_dir.exists())
        self.assertIsNone(self._row(self.record_id))
        tombstone_metadata = [
            candidate.stat()
            for candidate in owner_dir.glob(".reimbursement-cleanup-*")
        ]
        self.assertEqual(len(tombstone_metadata), 4)
        for first_metadata in first_tombstone_metadata:
            self.assertTrue(
                any(
                    os.path.samestat(first_metadata, metadata)
                    for metadata in tombstone_metadata
                )
            )

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

    def test_fresh_purge_close_failure_after_entry_removal_is_attempted_once(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        record_dir = self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        real_create = ReimbursementService._create_named_quarantine_directory
        real_close = reimbursements.os.close
        quarantine_fd = None
        close_attempts = 0

        def capture_quarantine(parent_fd, name):
            nonlocal quarantine_fd
            descriptor = real_create(parent_fd, name)
            if name.startswith(f".reimbursement-purge-{self.record_id}-"):
                quarantine_fd = descriptor
            return descriptor

        def fail_quarantine_close(descriptor):
            nonlocal close_attempts
            if descriptor == quarantine_fd:
                close_attempts += 1
                self.assertEqual(reimbursements.os.listdir(descriptor), [])
                raise OSError("forced persistent fresh quarantine close failure")
            return real_close(descriptor)

        try:
            with mock.patch.object(
                ReimbursementService,
                "_create_named_quarantine_directory",
                side_effect=capture_quarantine,
            ), mock.patch(
                "reimbursements.os.close",
                side_effect=fail_quarantine_close,
            ):
                service.purge_one(self.user_id, self.record_id)
        finally:
            if quarantine_fd is not None:
                real_close(quarantine_fd)

        self.assertEqual(close_attempts, 1)
        self.assertFalse(record_dir.exists())
        self.assertIsNone(self._row(self.record_id))

    def test_resumed_empty_close_failure_is_attempted_once(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        marker = self._create_empty_purge_quarantine(service)
        real_open = ReimbursementService._open_directory
        real_close = reimbursements.os.close
        marker_fd = None
        close_attempts = 0

        def capture_marker(parent_fd, name):
            nonlocal marker_fd
            descriptor = real_open(parent_fd, name)
            if name == marker.name and marker_fd is None:
                marker_fd = descriptor
            return descriptor

        def fail_marker_close(descriptor):
            nonlocal close_attempts
            if descriptor == marker_fd:
                close_attempts += 1
                self.assertEqual(reimbursements.os.listdir(descriptor), [])
                raise OSError("forced persistent resumed marker close failure")
            return real_close(descriptor)

        try:
            with mock.patch.object(
                ReimbursementService,
                "_open_directory",
                side_effect=capture_marker,
            ), mock.patch(
                "reimbursements.os.close",
                side_effect=fail_marker_close,
            ):
                service.purge_one(self.user_id, self.record_id)
        finally:
            if marker_fd is not None:
                real_close(marker_fd)

        self.assertEqual(close_attempts, 1)
        self.assertFalse(marker.exists())
        self.assertIsNone(self._row(self.record_id))

    def test_same_record_purge_workers_are_serialized_across_services(self):
        first = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        second = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        self._record_directory_with_files()
        first.trash(self.user_id, self.record_id)
        removal_started = threading.Event()
        allow_removal = threading.Event()
        second_attempting = threading.Event()
        second_entered = threading.Event()
        results = []

        def block_removal(_name, **_kwargs):
            removal_started.set()
            if not allow_removal.wait(timeout=2):
                raise TimeoutError("test did not release purge")

        first._remove_tree = block_removal
        real_second = second._purge_one_serialized

        def observe_second(*args, **kwargs):
            second_entered.set()
            return real_second(*args, **kwargs)

        second._purge_one_serialized = observe_second

        def purge(service, label):
            try:
                service.purge_one(self.user_id, self.record_id)
            except ReimbursementNotFound:
                results.append((label, "not-found"))
            else:
                results.append((label, "purged"))

        first_thread = threading.Thread(target=purge, args=(first, "first"))

        def purge_second():
            second_attempting.set()
            purge(second, "second")

        second_thread = threading.Thread(target=purge_second)
        first_thread.start()
        self.assertTrue(removal_started.wait(timeout=1))
        second_thread.start()
        self.assertTrue(second_attempting.wait(timeout=1))
        try:
            self.assertFalse(second_entered.wait(timeout=0.1))
        finally:
            allow_removal.set()
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(
            sorted(results),
            [("first", "purged"), ("second", "not-found")],
        )

    def test_post_commit_marker_is_not_adopted_by_reused_record_id(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)

        with mock.patch(
            "reimbursements.secrets.token_hex",
            side_effect=("1" * 64, "2" * 64),
        ):
            with mock.patch.object(
                service,
                "_remove_purge_completion_marker",
            ):
                service.purge_one(self.user_id, self.record_id)
            owner_dir = self.data_dir / "users" / str(self.user_id)
            old_markers = list(
                owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
            )
            self.assertEqual(len(old_markers), 1)
            self.assertIn("1" * 64, old_markers[0].name)

            self._insert_record(
                self.user_id,
                self.record_id,
                created_at="2026-09-08T01:00:00+00:00",
                deleted_at="2026-09-08T02:00:00+00:00",
                create_files=True,
            )
            service.purge_one(self.user_id, self.record_id)

        self.assertTrue(old_markers[0].exists())
        self.assertIsNone(self._row(self.record_id))
        self.assertEqual(
            list(owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")),
            old_markers,
        )

    def test_restore_ignores_and_retains_old_marker_for_reused_record_id(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        self._record_directory_with_files()
        service.trash(self.user_id, self.record_id)
        with mock.patch.object(service, "_remove_purge_completion_marker"):
            service.purge_one(self.user_id, self.record_id)

        owner_dir = self.data_dir / "users" / str(self.user_id)
        old_markers = list(
            owner_dir.glob(f".reimbursement-purge-{self.record_id}-*")
        )
        self.assertEqual(len(old_markers), 1)
        old_marker_metadata = old_markers[0].stat()
        record_dir = self._insert_record(
            self.user_id,
            self.record_id,
            created_at="2026-09-08T01:00:00+00:00",
            deleted_at="2026-09-08T02:00:00+00:00",
            create_files=True,
        )

        restored = service.restore(self.user_id, self.record_id)

        self.assertIsNone(restored.deleted_at)
        row = self._row(self.record_id)
        self.assertIsNotNone(row)
        self.assertIsNone(row["deleted_at"])
        self.assertIsNone(row["purge_claim"])
        self.assertEqual((record_dir / "claim.xlsx").read_bytes(), b"xlsx")
        self.assertEqual((record_dir / "claim.pdf").read_bytes(), b"pdf")
        self.assertTrue(old_markers[0].exists())
        self.assertTrue(os.path.samestat(old_marker_metadata, old_markers[0].stat()))

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
        with self.assertRaises(reimbursements.ReimbursementStorageError):
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

        with self.assertRaises(reimbursements.ReimbursementStorageError):
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
        claim = "c" * 64
        name = service._purge_quarantine_name(self.record_id, claim, identity)
        prefix = f".reimbursement-purge-{self.record_id}-"
        components = name[len(prefix):].split("-")

        self.assertEqual(len(components), 4)
        encoded_claim, device, inode, mac = components
        self.assertEqual(encoded_claim, claim)
        self.assertEqual(
            service._purge_quarantine_identity(name, self.record_id, claim),
            identity,
        )
        invalid_names = (
            f"{prefix}{'d' * 64}-{device}-{inode}-{mac}",
            f"{prefix}{claim}-0{device}-{inode}-{mac}",
            f"{prefix}{claim}-{device.upper()}-{inode}-{mac}",
            f"{prefix}{claim}-{device}-0{inode}-{mac}",
            f"{prefix}{claim}-{device}-{inode}-A{mac[1:]}",
            f"{prefix}{claim}-{device}-{inode}-{'0' if mac[0] != '0' else '1'}{mac[1:]}",
            f"{name}-extra",
        )
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=invalid_name):
                self.assertIsNone(
                    service._purge_quarantine_identity(
                        invalid_name,
                        self.record_id,
                        claim,
                    )
                )

    def test_purge_quarantine_mac_uses_constant_time_comparison(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        identity = (1, 2)
        claim = "c" * 64
        name = service._purge_quarantine_name(self.record_id, claim, identity)
        real_compare_digest = hmac.compare_digest

        with mock.patch(
            "hmac.compare_digest",
            wraps=real_compare_digest,
        ) as compare_digest:
            parsed = service._purge_quarantine_identity(
                name,
                self.record_id,
                claim,
            )

        self.assertEqual(parsed, identity)
        compare_digest.assert_called_once()

    def test_purge_completion_name_authenticates_state_and_both_identities(self):
        service = ReimbursementService(
            self.database,
            self.config,
            app_secret=b"a" * 32,
        )
        claim = "c" * 64
        record_identity = (0xABCDEF, 0x123ABC)
        marker_identity = (0x456DEF, 0x789ABC)

        name = service._purge_completion_name(
            self.record_id,
            claim,
            record_identity,
            marker_identity,
        )
        parsed = service._purge_completion_identity(
            name,
            self.record_id,
            claim,
        )

        self.assertEqual(parsed, (record_identity, marker_identity))
        self.assertIn(f"-{self.record_id}-complete-{claim}-", name)
        prefix = f".reimbursement-purge-{self.record_id}-"
        components = name[len(prefix):].split("-")
        self.assertEqual(len(components), 7)
        (
            state,
            encoded_claim,
            record_device,
            record_inode,
            marker_device,
            marker_inode,
            mac,
        ) = components
        invalid_names = (
            name.replace("-complete-", "-pending-", 1),
            f"{prefix}{state}-{'d' * 64}-{record_device}-{record_inode}-{marker_device}-{marker_inode}-{mac}",
            f"{prefix}{state}-{encoded_claim}-{record_device}-{record_inode}-{int(marker_device, 16) + 1:x}-{marker_inode}-{mac}",
            f"{prefix}{state}-{encoded_claim}-{record_device}-{record_inode}-{marker_device}-{marker_inode}-{'0' if mac[0] != '0' else '1'}{mac[1:]}",
        )
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=invalid_name):
                self.assertIsNone(
                    service._purge_completion_identity(
                        invalid_name,
                        self.record_id,
                        claim,
                    )
                )

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
            with self.assertRaises(reimbursements.ReimbursementStorageError):
                failing_service.purge_one(self.user_id, self.record_id)

        with self.assertRaises(ReimbursementNotFound):
            self.service.restore(self.user_id, self.record_id)

        row = self._row(self.record_id)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["deleted_at"])
        self.assertRegex(row["purge_claim"], "^[0-9a-f]{64}$")
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
            with self.assertRaises(reimbursements.ReimbursementStorageError):
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

        real_resume = self.service._resume_purge_at

        def fail_one(parent_fd, name, identity, record_id, claim, **kwargs):
            if record_id == failed_id:
                return None
            return real_resume(
                parent_fd,
                name,
                identity,
                record_id,
                claim,
                **kwargs,
            )

        with mock.patch.object(
            self.service,
            "_resume_purge_at",
            side_effect=fail_one,
        ):
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
        self.assertIsNone(row["purge_claim"])
        self.assertTrue(record_dir.exists())

    def test_expired_purge_stops_between_candidates(self):
        self.assertIn(
            "stop_event",
            inspect.signature(self.service.purge_expired).parameters,
        )
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        expired_at = (now - timedelta(days=31)).isoformat()
        for suffix in (11, 12, 13):
            self._insert_record(
                self.user_id,
                f"20000000-0000-4000-8000-{suffix:012d}",
                created_at=expired_at,
                deleted_at=expired_at,
            )
        stop_event = threading.Event()
        calls = []

        def stop_after_first(user_id, record_id, **kwargs):
            calls.append((user_id, record_id, kwargs))
            stop_event.set()
            return True

        with mock.patch.object(
            self.service,
            "_purge_one",
            side_effect=stop_after_first,
        ):
            purged = self.service.purge_expired(now=now, stop_event=stop_event)

        self.assertEqual(purged, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][2],
            {
                "deleted_at_or_before": (
                    now - timedelta(days=30)
                ).isoformat()
            },
        )

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

    def test_server_close_has_bounded_join_during_active_cleanup(self):
        cleanup_started = threading.Event()
        release_cleanup = threading.Event()
        purge_calls = 0

        def stall_background_purge(_service, *args, **kwargs):
            nonlocal purge_calls
            purge_calls += 1
            if purge_calls == 2:
                cleanup_started.set()
                release_cleanup.wait(timeout=0.5)
            return 0

        def run_cleanup_once(_stop_event, cleanup):
            cleanup()

        with mock.patch.object(
            app.ReimbursementService,
            "purge_expired",
            autospec=True,
            side_effect=stall_background_purge,
        ):
            server = app.create_server(
                self.config_path,
                cleanup_runner=run_cleanup_once,
            )
            try:
                self.assertTrue(cleanup_started.wait(timeout=1))
                started = time.monotonic()
                with mock.patch.object(
                    app,
                    "_CLEANUP_JOIN_TIMEOUT_SECONDS",
                    0.05,
                    create=True,
                ):
                    server.server_close()
                elapsed = time.monotonic() - started

                self.assertTrue(server.cleanup_stop_event.is_set())
                self.assertLess(elapsed, 0.2)
                self.assertTrue(server.cleanup_thread.is_alive())
            finally:
                release_cleanup.set()
                server.cleanup_thread.join(timeout=1)
                if server.fileno() != -1:
                    server.server_close()

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
        cleanup.assert_called_once_with(event)


if __name__ == "__main__":
    unittest.main()
