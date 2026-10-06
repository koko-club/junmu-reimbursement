"""Versioned Ed25519 Pro license tokens; no signing key is shipped with the app."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import json
import re
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


PRODUCT = "junmu-reimbursement"
PUBLIC_KEY_BASE64 = "wolYMgMEvcqTsB9mqfoe9IpelH13LRbwFWSxihtef2Q="
MAX_CODE_LENGTH = 1024
_SERIAL = re.compile(r"[0-9a-f]{32}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")
_EXPECTED_FIELDS = {"version", "product", "serial", "tier", "issued_at", "expires_at", "license_id"}


class InvalidLicense(ValueError):
    """License data failed validation or does not belong to this account."""


@dataclass(frozen=True)
class LicenseClaims:
    serial: str
    tier: str
    issued_at: datetime
    expires_at: datetime
    license_id: str


def production_public_key() -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_BASE64))


def sign_license(
    private_key: Ed25519PrivateKey, serial: str, *, issued_at: datetime | None = None
) -> str:
    if not isinstance(serial, str) or _SERIAL.fullmatch(serial) is None:
        raise InvalidLicense("invalid serial")
    issued = issued_at or datetime.now(timezone.utc)
    if issued.tzinfo is None or issued.utcoffset() is None:
        raise ValueError("issued_at must be timezone-aware")
    issued = issued.astimezone(timezone.utc).replace(microsecond=0)
    payload = {
        "version": 1,
        "product": PRODUCT,
        "serial": serial,
        "tier": "pro",
        "issued_at": _format_time(issued),
        "expires_at": _format_time(issued + timedelta(days=365)),
        "license_id": secrets.token_hex(16),
    }
    raw = _canonical(payload)
    signature = private_key.sign(raw)
    return _b64(raw) + "." + _b64(signature)


def verify_license(
    code: str, serial: str, public_key: Ed25519PublicKey | None = None
) -> LicenseClaims:
    if (
        not isinstance(code, str) or len(code) > MAX_CODE_LENGTH
        or _TOKEN.fullmatch(code) is None
        or not isinstance(serial, str) or _SERIAL.fullmatch(serial) is None
    ):
        raise InvalidLicense("invalid license format")
    encoded_payload, encoded_signature = code.split(".")
    try:
        raw = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
        if len(signature) != 64 or len(raw) > 600:
            raise ValueError("invalid lengths")
        (public_key or production_public_key()).verify(signature, raw)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != _EXPECTED_FIELDS:
            raise ValueError("invalid fields")
        if raw != _canonical(payload):
            raise ValueError("noncanonical payload")
        if payload["version"] != 1 or type(payload["version"]) is not int:
            raise ValueError("invalid version")
        if payload["product"] != PRODUCT or payload["tier"] != "pro":
            raise ValueError("invalid product or tier")
        if payload["serial"] != serial:
            raise ValueError("wrong account")
        license_id = payload["license_id"]
        if not isinstance(license_id, str) or _SERIAL.fullmatch(license_id) is None:
            raise ValueError("invalid license id")
        issued = _parse_time(payload["issued_at"])
        expires = _parse_time(payload["expires_at"])
        if expires != issued + timedelta(days=365):
            raise ValueError("invalid duration")
    except (InvalidSignature, ValueError, TypeError, KeyError, UnicodeError) as error:
        raise InvalidLicense("invalid license") from error
    return LicenseClaims(serial, "pro", issued, expires, license_id)


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    result = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64(result) != value:
        raise ValueError("noncanonical base64")
    return result


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value) is None:
        raise ValueError("invalid timestamp")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if _format_time(parsed) != value:
        raise ValueError("invalid timestamp")
    return parsed
