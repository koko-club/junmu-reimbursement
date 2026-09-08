"""SQLite storage and schema migrations for the reimbursement application."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Callable, Iterator


_SCHEMA_V1 = (
    """CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash BLOB NOT NULL,
    password_salt BLOB NOT NULL,
    password_params TEXT NOT NULL,
    real_name TEXT NOT NULL,
    department TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'disabled')),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    updated_at TEXT NOT NULL
);""",
    """CREATE TABLE sessions (
    token_hash BLOB PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);""",
    """CREATE TABLE reimbursements (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reimbursement_date TEXT,
    display_name TEXT NOT NULL,
    xlsx_path TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);""",
    """CREATE INDEX reimbursements_owner_created
    ON reimbursements(user_id, deleted_at, created_at DESC);""",
    """CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);""",
)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            try:
                connection.close()
            except BaseException:
                pass
            raise
        return connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            try:
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise
        finally:
            connection.close()

    def delete_claimed_reimbursement_if(
        self,
        *,
        record_id: str,
        user_id: int,
        purge_claim: str,
        checker: Callable[[], bool],
        deleted_at_or_before: str | None = None,
    ) -> bool:
        """Check then delete on an unexposed writer connection.

        This closes application-level callback boundaries, but SQLite cannot
        serialize a separate process that writes directly to the data directory.
        """
        statement = """DELETE FROM reimbursements
            WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL
            AND purge_claim = ?"""
        parameters: tuple[object, ...] = (record_id, user_id, purge_claim)
        if deleted_at_or_before is not None:
            statement += " AND deleted_at <= ?"
            parameters += (deleted_at_or_before,)

        connection = self.connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not checker():
                    connection.rollback()
                    return False
                deleted = connection.execute(statement, parameters)
                if deleted.rowcount != 1:
                    connection.rollback()
                    return False
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise
            else:
                try:
                    connection.commit()
                except Exception:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                    raise
                return True
        finally:
            connection.close()

    def migrate(self) -> None:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )"""
            )
            version = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 1"
            ).fetchone()
            if version is None:
                for statement in _SCHEMA_V1:
                    connection.execute(statement)
                connection.execute(
                    "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('setup_complete', 'false')"
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, datetime.now(timezone.utc).isoformat()),
                )
            version = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 2"
            ).fetchone()
            if version is None:
                columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(users)")
                }
                if "status_version" not in columns:
                    connection.execute(
                        "ALTER TABLE users ADD COLUMN status_version INTEGER NOT NULL DEFAULT 0"
                    )
                if "password_version" not in columns:
                    connection.execute(
                        "ALTER TABLE users ADD COLUMN password_version INTEGER NOT NULL DEFAULT 0"
                    )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, datetime.now(timezone.utc).isoformat()),
                )
            version = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 3"
            ).fetchone()
            if version is None:
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(reimbursements)")
                }
                if "purge_claim" not in columns:
                    connection.execute(
                        """ALTER TABLE reimbursements ADD COLUMN purge_claim TEXT
                        CHECK (
                            purge_claim IS NULL OR (
                                length(purge_claim) = 64
                                AND purge_claim NOT GLOB '*[^0-9a-f]*'
                            )
                        )"""
                    )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, datetime.now(timezone.utc).isoformat()),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def backup(self, destination: Path) -> None:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with ExitStack() as cleanup:
            source = self.connect()
            cleanup.callback(source.close)
            target = sqlite3.connect(destination_path)
            cleanup.callback(target.close)
            source.backup(target)
