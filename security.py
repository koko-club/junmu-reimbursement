"""Local authentication primitives with no persistence or request handling."""

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time


_SECRET_LENGTH = 32
_SALT_LENGTH = 16
_DIGEST_LENGTH = 32
_SECRET_READ_ATTEMPTS = 100
_MAX_SCRYPT_MEMORY = 64 * 1024 * 1024


@dataclass(frozen=True)
class PasswordMaterial:
    digest: bytes
    salt: bytes
    params: str


class PasswordHasher:
    """Hash passwords with scrypt and retain the work factors per digest."""

    def __init__(self, n: int = 16384, r: int = 8, p: int = 1):
        self._params = self._validate_params({"n": n, "r": r, "p": p})

    def hash(self, password: str) -> PasswordMaterial:
        password_bytes = self._password_bytes(password)
        salt = secrets.token_bytes(_SALT_LENGTH)
        digest = self._scrypt(password_bytes, salt, self._params)
        return PasswordMaterial(
            digest=digest,
            salt=salt,
            params=json.dumps(self._params, separators=(",", ":"), sort_keys=True),
        )

    def verify(self, password: str, material: PasswordMaterial) -> bool:
        try:
            password_bytes = self._password_bytes(password)
            if not isinstance(material, PasswordMaterial):
                return False
            if not isinstance(material.digest, bytes) or len(material.digest) != _DIGEST_LENGTH:
                return False
            if not isinstance(material.salt, bytes) or len(material.salt) != _SALT_LENGTH:
                return False
            if not isinstance(material.params, str):
                return False
            params = self._validate_params(json.loads(material.params))
            candidate = self._scrypt(password_bytes, material.salt, params)
        except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
            return False
        return hmac.compare_digest(candidate, material.digest)

    @staticmethod
    def _password_bytes(password: str) -> bytes:
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError("password must contain 8 to 128 Unicode characters")
        return password.encode("utf-8")

    @staticmethod
    def _validate_params(params: object) -> dict[str, int]:
        if not isinstance(params, dict) or set(params) != {"n", "r", "p"}:
            raise ValueError("invalid scrypt parameters")
        n = params["n"]
        r = params["r"]
        p = params["p"]
        if (
            type(n) is not int
            or type(r) is not int
            or type(p) is not int
            or n < 2
            or n & (n - 1)
            or r <= 0
            or p <= 0
            or 128 * n * r > _MAX_SCRYPT_MEMORY
            or r * p >= 2**30
        ):
            raise ValueError("invalid scrypt parameters")
        return {"n": n, "r": r, "p": p}

    @staticmethod
    def _scrypt(password: bytes, salt: bytes, params: dict[str, int]) -> bytes:
        return hashlib.scrypt(
            password,
            salt=salt,
            n=params["n"],
            r=params["r"],
            p=params["p"],
            dklen=_DIGEST_LENGTH,
            maxmem=_MAX_SCRYPT_MEMORY,
        )


def load_or_create_secret(path: str | os.PathLike[str]) -> bytes:
    """Return a 32-byte secret created once at *path* with owner-only access."""
    secret_path = Path(path)
    secret_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    while True:
        try:
            return _read_complete_secret(secret_path)
        except FileNotFoundError:
            pass

        descriptor = None
        temporary_path = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{secret_path.name}.",
                suffix=".tmp",
                dir=secret_path.parent,
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            secret = secrets.token_bytes(_SECRET_LENGTH)
            _write_all(descriptor, secret)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary_path, secret_path)
            except FileExistsError:
                return _read_complete_secret(secret_path)
            _fsync_directory(secret_path.parent)
            return secret
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_path is not None:
                _remove_created_secret(temporary_path)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("could not write secret file")
        view = view[written:]


def _remove_created_secret(path: Path) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_complete_secret(path: Path) -> bytes:
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        read_flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW

    for attempt in range(_SECRET_READ_ATTEMPTS):
        descriptor = os.open(path, read_flags)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o077:
                raise ValueError("secret file is not private and regular")
            if details.st_size > _SECRET_LENGTH:
                raise ValueError("secret file has an invalid length")
            secret = _read_all(descriptor, _SECRET_LENGTH)
        finally:
            os.close(descriptor)

        if len(secret) == _SECRET_LENGTH:
            return secret
        if attempt + 1 < _SECRET_READ_ATTEMPTS:
            time.sleep(0.01)
    raise ValueError("secret file is incomplete")


def _read_all(descriptor: int, expected_length: int) -> bytes:
    chunks = []
    remaining = expected_length
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class AnonymousCsrfSigner:
    """Issue and validate short-lived CSRF tokens before a user has a session."""

    def __init__(self, secret: bytes, ttl: int = 1800):
        if not isinstance(secret, bytes) or len(secret) != _SECRET_LENGTH:
            raise ValueError("CSRF secret must be 32 bytes")
        if type(ttl) is not int or ttl <= 0:
            raise ValueError("CSRF ttl must be positive")
        self._secret = secret
        self._ttl = ttl

    def issue(self, method: str, path: str, now: int | float | None = None) -> str:
        method, path = self._request_target(method, path)
        issued_at = self._timestamp(now)
        nonce = secrets.token_urlsafe(16)
        signature = self._signature(issued_at, method, path, nonce)
        return f"{issued_at}.{nonce}.{signature}"

    def verify(self, token: object, method: str, path: str, now: int | float | None = None) -> bool:
        try:
            method, path = self._request_target(method, path)
            current_time = self._timestamp(now)
            if not isinstance(token, str):
                return False
            issued_text, nonce, supplied_signature = token.split(".")
            if (
                not issued_text.isascii()
                or not issued_text.isdecimal()
                or not nonce
                or not all(character.isascii() and (character.isalnum() or character in "_-") for character in nonce)
                or len(supplied_signature) != 64
                or not all(character in "0123456789abcdef" for character in supplied_signature)
            ):
                return False
            issued_at = int(issued_text)
            if issued_at > current_time or current_time - issued_at > self._ttl:
                return False
            expected_signature = self._signature(issued_at, method, path, nonce)
            return hmac.compare_digest(expected_signature, supplied_signature)
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _request_target(method: str, path: str) -> tuple[str, str]:
        if not isinstance(method, str) or not method or not isinstance(path, str):
            raise ValueError("method and path must be strings")
        return method.upper(), path

    @staticmethod
    def _timestamp(now: int | float | None) -> int:
        if now is None:
            return int(time.time())
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("timestamp must be finite")
        return int(now)

    def _signature(self, issued_at: int, method: str, path: str, nonce: str) -> str:
        message = json.dumps(
            [issued_at, method, path, nonce],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()
