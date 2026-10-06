"""Persistent account license state and server-clock rollback checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from database import Database
from license import InvalidLicense, production_public_key, verify_license


class ClockRollback(ValueError):
    pass


@dataclass(frozen=True)
class LicenseState:
    serial: str
    tier: str
    expires_at: str | None
    reason: str | None


class LicenseService:
    def __init__(self, database: Database, public_key: Ed25519PublicKey | None = None):
        self._database = database
        self._public_key = public_key or production_public_key()

    def status(self, user_id: int, *, now: datetime | None = None) -> LicenseState:
        current = _utc(now)
        with self._database.transaction(immediate=True) as connection:
            row = self._user(connection, user_id)
            if self._clock_rolled_back(row, current):
                return LicenseState(row["license_serial"], "basic", None, "clock_rollback")
            self._advance_clock(connection, user_id, row, current)
            return self._evaluate(row, current)

    def import_code(self, user_id: int, code: str, *, now: datetime | None = None) -> LicenseState:
        current = _utc(now)
        with self._database.transaction(immediate=True) as connection:
            row = self._user(connection, user_id)
            if row["role"] != "user" or row["status"] != "active":
                raise InvalidLicense("active user required")
            if self._clock_rolled_back(row, current):
                raise ClockRollback("server clock moved backwards")
            claims = verify_license(code, row["license_serial"], self._public_key)
            if current < claims.issued_at - timedelta(minutes=5) or current >= claims.expires_at:
                raise InvalidLicense("license not currently valid")
            old_code = row["license_code"]
            if old_code:
                try:
                    old = verify_license(old_code, row["license_serial"], self._public_key)
                except InvalidLicense:
                    old = None
                if old is not None and old.expires_at > claims.expires_at:
                    raise InvalidLicense("older license")
            connection.execute(
                "UPDATE users SET license_code = ?, license_last_seen_at = ? WHERE id = ?",
                (code, current.isoformat(), user_id),
            )
            return LicenseState(row["license_serial"], "pro", claims.expires_at.isoformat(), None)

    def reset_clock(self, serial: str) -> None:
        """Local operator recovery after the server clock has been corrected."""
        if not isinstance(serial, str) or re.fullmatch(r"[0-9a-f]{32}", serial) is None:
            raise InvalidLicense("invalid serial")
        with self._database.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE users SET license_last_seen_at = NULL WHERE license_serial = ?",
                (serial,),
            )
            if updated.rowcount != 1:
                raise InvalidLicense("account unavailable")

    @staticmethod
    def _user(connection: sqlite3.Connection, user_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id, role, status, license_serial, license_code, license_last_seen_at "
            "FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or not row["license_serial"]:
            raise InvalidLicense("account unavailable")
        return row

    @staticmethod
    def _clock_rolled_back(row: sqlite3.Row, current: datetime) -> bool:
        last = row["license_last_seen_at"]
        return bool(last and current < datetime.fromisoformat(last) - timedelta(minutes=5))

    @staticmethod
    def _advance_clock(connection: sqlite3.Connection, user_id: int, row: sqlite3.Row, current: datetime) -> None:
        last = row["license_last_seen_at"]
        if last is None or current > datetime.fromisoformat(last):
            connection.execute(
                "UPDATE users SET license_last_seen_at = ? WHERE id = ?",
                (current.isoformat(), user_id),
            )

    def _evaluate(self, row: sqlite3.Row, current: datetime) -> LicenseState:
        code = row["license_code"]
        if not code:
            return LicenseState(row["license_serial"], "basic", None, None)
        try:
            claims = verify_license(code, row["license_serial"], self._public_key)
        except InvalidLicense:
            return LicenseState(row["license_serial"], "basic", None, "invalid")
        expiry = claims.expires_at.isoformat()
        if current < claims.issued_at - timedelta(minutes=5):
            return LicenseState(row["license_serial"], "basic", expiry, "not_yet_valid")
        if current >= claims.expires_at:
            return LicenseState(row["license_serial"], "basic", expiry, "expired")
        return LicenseState(row["license_serial"], "pro", expiry, None)


def _utc(now: datetime | None) -> datetime:
    result = now or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timezone-aware time required")
    return result.astimezone(timezone.utc)
