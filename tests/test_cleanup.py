from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
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
