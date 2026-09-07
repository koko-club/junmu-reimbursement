"""SQLite storage and schema migrations for the reimbursement application."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator


_SCHEMA_V1 = (
    """CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    password_params TEXT NOT NULL,
    real_name TEXT NOT NULL,
    department TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'disabled')),
    must_change_password INTEGER NOT NULL CHECK (must_change_password IN (0, 1)),
    created_at TEXT NOT NULL,
    approved_at TEXT,
    updated_at TEXT NOT NULL
);""",
    """CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);""",
    """CREATE TABLE reimbursements (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reimbursement_date TEXT,
    display_name TEXT NOT NULL,
    xlsx_path TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT
);""",
    """CREATE INDEX idx_reimbursements_owner_deleted_created
    ON reimbursements(user_id, deleted_at, created_at);""",
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
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
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
            connection.commit()
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
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def backup(self, destination: Path) -> None:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        target = sqlite3.connect(destination_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
