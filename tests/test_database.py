from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from database import Database


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "state" / "app.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_connect_creates_configured_connection(self):
        connection = self.db.connect()
        try:
            self.assertIs(connection.row_factory, sqlite3.Row)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            connection.close()

    def test_migrate_is_idempotent_and_records_version(self):
        self.db.migrate()
        self.db.migrate()

        with self.db.transaction() as connection:
            versions = connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            setting = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'setup_complete'"
            ).fetchone()

        self.assertEqual([row["version"] for row in versions], [1])
        self.assertTrue(versions[0]["applied_at"])
        self.assertEqual(setting["value"], "false")

    def test_v1_schema_matches_storage_contract(self):
        self.db.migrate()

        with self.db.transaction() as connection:
            users = {
                row["name"]: row
                for row in connection.execute("PRAGMA table_info(users)")
            }
            sessions = {
                row["name"]: row
                for row in connection.execute("PRAGMA table_info(sessions)")
            }
            reimbursements = {
                row["name"]: row
                for row in connection.execute("PRAGMA table_info(reimbursements)")
            }
            indexes = {
                row["name"]
                for row in connection.execute("PRAGMA index_list(reimbursements)")
            }
            indexed_columns = [
                (row["name"], row["desc"])
                for row in connection.execute("PRAGMA index_xinfo(reimbursements_owner_created)")
                if row["key"]
            ]

        self.assertEqual(users["password_hash"]["type"], "BLOB")
        self.assertEqual(users["password_salt"]["type"], "BLOB")
        self.assertEqual(users["must_change_password"]["dflt_value"], "0")
        self.assertEqual(sessions["token_hash"]["type"], "BLOB")
        self.assertEqual(reimbursements["id"]["type"], "TEXT")
        self.assertEqual(reimbursements["id"]["pk"], 1)
        self.assertIn("reimbursements_owner_created", indexes)
        self.assertEqual(
            indexed_columns,
            [("user_id", 0), ("deleted_at", 0), ("created_at", 1)],
        )

    def test_transaction_rolls_back_when_body_raises(self):
        self.db.migrate()

        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO app_settings(key, value) VALUES (?, ?)",
                    ("temporary", "value"),
                )
                raise RuntimeError("abort")

        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'temporary'"
            ).fetchone()
        self.assertIsNone(row)

    def test_foreign_keys_cascade_sessions_and_restrict_reimbursements(self):
        self.db.migrate()
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO users(
                    username, username_key, password_hash, password_salt, password_params,
                    real_name, department, role, status, must_change_password,
                    created_at, approved_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("alice", "alice", "hash", "salt", "params", "Alice", "IT", "user",
                 "active", 0, "2026-09-07T00:00:00+00:00", None, "2026-09-07T00:00:00+00:00"),
            )
            user_id = connection.execute("SELECT id FROM users WHERE username_key = 'alice'").fetchone()["id"]
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                ("token", user_id, "csrf", "created", "expires"),
            )
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.assertIsNone(connection.execute("SELECT * FROM sessions WHERE token_hash = 'token'").fetchone())

        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO users(
                    username, username_key, password_hash, password_salt, password_params,
                    real_name, department, role, status, must_change_password,
                    created_at, approved_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("bob", "bob", "hash", "salt", "params", "Bob", "IT", "user",
                 "active", 0, "created", None, "updated"),
            )
            user_id = connection.execute("SELECT id FROM users WHERE username_key = 'bob'").fetchone()["id"]
            connection.execute(
                """INSERT INTO reimbursements(
                    user_id, reimbursement_date, display_name, xlsx_path, pdf_path, created_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, None, "form", "form.xlsx", "form.pdf", "created", None),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def test_backup_creates_parent_directory_and_copies_data(self):
        self.db.migrate()
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO app_settings(key, value) VALUES (?, ?)", ("sample", "saved"))

        destination = self.root / "backups" / "app.sqlite3"
        self.db.backup(destination)

        self.assertTrue(destination.is_file())
        copied = sqlite3.connect(destination)
        try:
            self.assertEqual(
                copied.execute("SELECT value FROM app_settings WHERE key = 'sample'").fetchone()[0],
                "saved",
            )
        finally:
            copied.close()
