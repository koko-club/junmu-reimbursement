"""Persistent, single-session authentication state without HTTP concerns."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Callable

from database import Database


_SESSION_LIFETIME = timedelta(days=7)


@dataclass(frozen=True)
class IssuedSession:
    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: int
    username: str
    real_name: str
    department: str
    role: str
    must_change_password: bool
    csrf_token: str


class SessionService:
    """Issue and resolve one active session per active account."""

    def __init__(
        self, database: Database, clock: Callable[[], datetime] | None = None
    ):
        self._database = database
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, user_id: int, now: datetime | None = None) -> IssuedSession:
        if not _valid_user_id(user_id):
            raise ValueError("active user required")
        issued_at = self._now(now)
        expires_at = issued_at + _SESSION_LIFETIME
        with self._database.transaction(immediate=True) as connection:
            user = connection.execute(
                "SELECT id FROM users WHERE id = ? AND status = 'active'", (user_id,)
            ).fetchone()
            if user is None:
                raise ValueError("active user required")
            token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            token_hash = _token_hash(token)
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            connection.execute(
                """INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    token_hash, user_id, csrf_token, issued_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return IssuedSession(token=token, csrf_token=csrf_token, expires_at=expires_at)

    def resolve(
        self, token: object, now: datetime | None = None
    ) -> AuthenticatedUser | None:
        token_hash = _safe_token_hash(token)
        if token_hash is None:
            return None
        current_time = self._now(now)
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT sessions.user_id, sessions.csrf_token, sessions.expires_at,
                    users.username, users.real_name, users.department, users.role,
                    users.status, users.must_change_password
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?""",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            try:
                expires_at = _from_utc_iso(row["expires_at"])
            except (TypeError, ValueError):
                expires_at = current_time
            if row["status"] != "active" or current_time >= expires_at:
                connection.execute(
                    "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
                )
                return None
            return AuthenticatedUser(
                user_id=row["user_id"],
                username=row["username"],
                real_name=row["real_name"],
                department=row["department"],
                role=row["role"],
                must_change_password=bool(row["must_change_password"]),
                csrf_token=row["csrf_token"],
            )

    @staticmethod
    def verify_csrf(user: object, submitted: object) -> bool:
        if (
            not isinstance(user, AuthenticatedUser)
            or not isinstance(submitted, str)
            or not submitted
            or not isinstance(user.csrf_token, str)
            or not user.csrf_token
        ):
            return False
        return hmac.compare_digest(user.csrf_token, submitted)

    def revoke_token(self, token: object) -> None:
        token_hash = _safe_token_hash(token)
        if token_hash is None:
            return
        with self._database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def revoke_user(self, user_id: object) -> None:
        if not _valid_user_id(user_id):
            return
        with self._database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def purge_expired(self, now: datetime | None = None) -> int:
        current_time = self._now(now)
        with self._database.transaction(immediate=True) as connection:
            deleted = connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (current_time.isoformat(),)
            )
        return deleted.rowcount

    def _now(self, supplied: datetime | None) -> datetime:
        current_time = self._clock() if supplied is None else supplied
        if not isinstance(current_time, datetime):
            raise TypeError("clock must return a datetime")
        if current_time.tzinfo is None:
            raise ValueError("time must be timezone-aware")
        return current_time.astimezone(timezone.utc)


def _valid_user_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _safe_token_hash(token: object) -> bytes | None:
    if not isinstance(token, str) or not token:
        return None
    try:
        return _token_hash(token)
    except UnicodeEncodeError:
        return None


def _from_utc_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)
