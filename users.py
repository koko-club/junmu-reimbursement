"""Persistent user lifecycle rules for local reimbursement access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import secrets
import sqlite3
from typing import Callable
import unicodedata

from database import Database
from security import PasswordHasher, PasswordMaterial


_LOGGER = logging.getLogger(__name__)
_PROFILE_FORMULA_PREFIXES = ("=", "+", "-", "@")


class UserError(Exception):
    """Base class for user lifecycle errors safe to expose to callers."""


class ValidationError(UserError):
    pass


class SetupClosed(UserError):
    pass


class SetupRequired(UserError):
    pass


class UsernameTaken(UserError):
    pass


class AuthenticationFailed(UserError):
    pass


class PermissionDenied(UserError):
    pass


class InvalidState(UserError):
    pass


class UserNotFound(UserError):
    pass


@dataclass(frozen=True)
class User:
    id: int
    username: str
    real_name: str
    department: str
    role: str
    status: str
    must_change_password: bool
    created_at: str
    approved_at: str | None
    updated_at: str
    status_version: int
    password_version: int


class UserService:
    """Create, administer, and authenticate users without request/session concerns."""

    def __init__(
        self,
        database: Database,
        password_hasher: PasswordHasher,
        revoke_sessions: Callable[[int], None] | None = None,
    ):
        self._database = database
        self._password_hasher = password_hasher
        self._revoke_sessions = revoke_sessions or (lambda _user_id: None)

    def setup_complete(self) -> bool:
        with self._database.transaction() as connection:
            setting = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'setup_complete'"
            ).fetchone()
        return setting is not None and setting["value"] == "true"

    def setup_admin(
        self, username: str, password: str, real_name: str, department: str
    ) -> User:
        username, username_key = self._username(username)
        real_name = self._profile_value(real_name, "real name")
        department = self._profile_value(department, "department")
        material = self._password_hasher.hash(password)
        now = _utc_now()
        with self._database.transaction(immediate=True) as connection:
            setting = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'setup_complete'"
            ).fetchone()
            if setting is None or setting["value"] != "false":
                raise SetupClosed("initial setup is already complete")
            try:
                cursor = connection.execute(
                    _INSERT_USER,
                    (
                        username, username_key, material.digest, material.salt, material.params,
                        real_name, department, "admin", "active", 0, now, None, now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise UsernameTaken("username is already in use") from error
            connection.execute(
                "UPDATE app_settings SET value = 'true' WHERE key = 'setup_complete'"
            )
            return self._get_in(connection, cursor.lastrowid)

    def register(
        self, username: str, password: str, real_name: str, department: str
    ) -> User:
        username, username_key = self._username(username)
        real_name = self._profile_value(real_name, "real name")
        department = self._profile_value(department, "department")
        material = self._password_hasher.hash(password)
        now = _utc_now()
        with self._database.transaction(immediate=True) as connection:
            setting = connection.execute(
                "SELECT value FROM app_settings WHERE key = 'setup_complete'"
            ).fetchone()
            if setting is None or setting["value"] != "true":
                raise SetupRequired("initial setup has not completed")
            try:
                cursor = connection.execute(
                    _INSERT_USER,
                    (
                        username, username_key, material.digest, material.salt, material.params,
                        real_name, department, "user", "pending", 0, now, None, now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise UsernameTaken("username is already in use") from error
            return self._get_in(connection, cursor.lastrowid)

    def authenticate(self, username: str, password: str) -> User:
        try:
            _, username_key = self._username(username)
        except ValidationError as error:
            raise AuthenticationFailed("invalid username or password") from error
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username_key = ?", (username_key,)
            ).fetchone()
        if row is None or row["status"] != "active":
            raise AuthenticationFailed("invalid username or password")
        material = PasswordMaterial(
            digest=bytes(row["password_hash"]),
            salt=bytes(row["password_salt"]),
            params=row["password_params"],
        )
        if not self._password_hasher.verify(password, material):
            raise AuthenticationFailed("invalid username or password")
        return self._public_user(row)

    def get(self, user_id: int) -> User:
        with self._database.transaction() as connection:
            return self._get_in(connection, user_id)

    def list_pending(self, admin_id: int) -> list[User]:
        with self._database.transaction(immediate=True) as connection:
            self._require_admin(connection, admin_id)
            rows = connection.execute(
                "SELECT * FROM users WHERE role = 'user' AND status = 'pending' "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return [self._public_user(row) for row in rows]

    def list_approved_users(self, admin_id: int) -> list[User]:
        with self._database.transaction(immediate=True) as connection:
            self._require_admin(connection, admin_id)
            rows = connection.execute(
                "SELECT * FROM users WHERE role = 'user' AND status IN ('active', 'disabled') "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return [self._public_user(row) for row in rows]

    def approve(self, admin_id: int, user_id: int) -> User:
        now = _utc_now()
        with self._database.transaction(immediate=True) as connection:
            self._require_admin(connection, admin_id)
            self._require_user_in_state(connection, user_id, "pending")
            connection.execute(
                "UPDATE users SET status = 'active', approved_at = ?, updated_at = ? WHERE id = ?",
                (now, now, user_id),
            )
            return self._get_in(connection, user_id)

    def reject(self, admin_id: int, user_id: int) -> None:
        with self._database.transaction(immediate=True) as connection:
            self._require_admin(connection, admin_id)
            self._require_user_in_state(connection, user_id, "pending")
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def update_profile(
        self, admin_id: int, user_id: int, real_name: str, department: str
    ) -> User:
        """Update the future-facing profile of an approved user."""
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValidationError("user id must be an integer")
        real_name = self._profile_value(real_name, "real name")
        department = self._profile_value(department, "department")
        now = _utc_now()
        with self._database.transaction(immediate=True) as connection:
            self._require_admin(connection, admin_id)
            self._require_user_approved(connection, user_id)
            connection.execute(
                "UPDATE users SET real_name = ?, department = ?, updated_at = ? WHERE id = ?",
                (real_name, department, now, user_id),
            )
            return self._get_in(connection, user_id)

    def set_enabled(self, admin_id: int, user_id: int, enabled: bool) -> User:
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be a boolean")
        desired_status = "active" if enabled else "disabled"
        changed_at = _utc_now()
        with self._database.transaction(immediate=True) as connection:
            self._require_admin(connection, admin_id)
            user = self._require_user_approved(connection, user_id)
            if user["status"] == desired_status:
                raise InvalidState("user already has requested enabled state")
            connection.execute(
                "UPDATE users SET status = ?, updated_at = ?, status_version = status_version + 1 "
                "WHERE id = ?",
                (desired_status, changed_at, user_id),
            )
            updated = self._get_in(connection, user_id)
            prior_status = user["status"]
        if not enabled:
            try:
                self._revoke(user_id)
            except Exception:
                self._restore_status(user_id, prior_status, updated.status_version)
                raise
        return updated

    def reset_password(self, admin_id: int, user_id: int) -> str:
        temporary_password = secrets.token_urlsafe(12)
        material = self._password_hasher.hash(temporary_password)
        changed_at = _utc_now()
        with self._database.transaction(immediate=True) as connection:
            self._require_admin(connection, admin_id)
            previous = self._require_user_approved(connection, user_id)
            connection.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, password_params = ?, "
                "must_change_password = 1, updated_at = ?, password_version = password_version + 1 "
                "WHERE id = ?",
                (material.digest, material.salt, material.params, changed_at, user_id),
            )
            updated = self._get_in(connection, user_id)
        try:
            self._revoke(user_id)
        except Exception:
            self._restore_password(
                user_id, previous, material, replacement_must_change_password=1,
                replacement_password_version=updated.password_version,
            )
            raise
        return temporary_password

    def change_password(
        self, user_id: int, current_password: str, new_password: str
    ) -> None:
        material = self._password_hasher.hash(new_password)
        changed_at = _utc_now()
        with self._database.transaction(immediate=True) as connection:
            user = self._user_row(connection, user_id)
            if user is None or user["role"] != "user" or user["status"] not in ("active", "disabled"):
                raise AuthenticationFailed("invalid username or password")
            old_material = PasswordMaterial(
                digest=bytes(user["password_hash"]),
                salt=bytes(user["password_salt"]),
                params=user["password_params"],
            )
            if not self._password_hasher.verify(current_password, old_material):
                raise AuthenticationFailed("invalid username or password")
            connection.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, password_params = ?, "
                "must_change_password = 0, updated_at = ?, password_version = password_version + 1 "
                "WHERE id = ?",
                (material.digest, material.salt, material.params, changed_at, user_id),
            )
            updated = self._get_in(connection, user_id)
        try:
            self._revoke(user_id)
        except Exception:
            self._restore_password(
                user_id, user, material, replacement_must_change_password=0,
                replacement_password_version=updated.password_version,
            )
            raise

    def _revoke(self, user_id: int) -> None:
        try:
            self._revoke_sessions(user_id)
        except Exception:
            _LOGGER.exception("session revocation failed for user_id=%s", user_id)
            raise

    def _restore_status(
        self, user_id: int, prior_status: str, replacement_status_version: int
    ) -> None:
        with self._database.transaction(immediate=True) as connection:
            restored = connection.execute(
                "UPDATE users SET status = ?, status_version = status_version + 1 "
                "WHERE id = ? AND status_version = ? AND status = 'disabled'",
                (prior_status, user_id, replacement_status_version),
            )
        if restored.rowcount != 1:
            _LOGGER.error("could not restore status after failed revocation for user_id=%s", user_id)

    def _restore_password(
        self,
        user_id: int,
        previous: sqlite3.Row,
        replacement: PasswordMaterial,
        replacement_must_change_password: int,
        replacement_password_version: int,
    ) -> None:
        with self._database.transaction(immediate=True) as connection:
            restored = connection.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, password_params = ?, "
                "must_change_password = ?, password_version = password_version + 1 "
                "WHERE id = ? AND password_hash = ? AND password_salt = ? AND password_params = ? "
                "AND must_change_password = ? AND password_version = ?",
                (
                    previous["password_hash"], previous["password_salt"], previous["password_params"],
                    previous["must_change_password"], user_id, replacement.digest,
                    replacement.salt, replacement.params, replacement_must_change_password,
                    replacement_password_version,
                ),
            )
        if restored.rowcount != 1:
            _LOGGER.error("could not restore password after failed revocation for user_id=%s", user_id)

    @staticmethod
    def _username(value: object) -> tuple[str, str]:
        if not isinstance(value, str):
            raise ValidationError("username must be a string")
        username = value.strip()
        key = unicodedata.normalize("NFKC", username).strip().casefold()
        if not 3 <= len(username) <= 50 or not key:
            raise ValidationError("username must contain 3 to 50 characters")
        return username, key

    @staticmethod
    def _profile_value(value: object, label: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{label} must be a string")
        value = value.strip()
        if not value or len(value) > 100 or value.startswith(_PROFILE_FORMULA_PREFIXES):
            raise ValidationError(f"invalid {label}")
        return value

    @staticmethod
    def _public_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"], username=row["username"], real_name=row["real_name"],
            department=row["department"], role=row["role"], status=row["status"],
            must_change_password=bool(row["must_change_password"]), created_at=row["created_at"],
            approved_at=row["approved_at"], updated_at=row["updated_at"],
            status_version=row["status_version"], password_version=row["password_version"],
        )

    def _get_in(self, connection: sqlite3.Connection, user_id: int) -> User:
        row = self._user_row(connection, user_id)
        if row is None:
            raise UserNotFound("user not found")
        return self._public_user(row)

    @staticmethod
    def _user_row(connection: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None
        return connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def _require_admin(self, connection: sqlite3.Connection, user_id: int) -> sqlite3.Row:
        user = self._user_row(connection, user_id)
        if user is None or user["role"] != "admin" or user["status"] != "active":
            raise PermissionDenied("active administrator required")
        return user

    def _require_user_in_state(
        self, connection: sqlite3.Connection, user_id: int, state: str
    ) -> sqlite3.Row:
        user = self._user_row(connection, user_id)
        if user is None or user["role"] != "user" or user["status"] != state:
            raise InvalidState("user is not in the required state")
        return user

    def _require_user_approved(self, connection: sqlite3.Connection, user_id: int) -> sqlite3.Row:
        user = self._user_row(connection, user_id)
        if user is None or user["role"] != "user" or user["status"] not in ("active", "disabled"):
            raise InvalidState("approved user required")
        return user


_INSERT_USER = """
    INSERT INTO users(
        username, username_key, password_hash, password_salt, password_params,
        real_name, department, role, status, must_change_password,
        created_at, approved_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
