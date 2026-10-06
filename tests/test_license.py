from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from database import Database
from license import InvalidLicense, sign_license, verify_license
from license_service import ClockRollback, LicenseService
from security import PasswordHasher
from users import UserService


NOW = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)


class SignedLicenseTest(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.serial = "a" * 32

    def test_sign_and_verify_365_day_license(self):
        code = sign_license(self.key, self.serial, issued_at=NOW)
        claims = verify_license(code, self.serial, self.key.public_key())
        self.assertEqual(claims.issued_at, NOW)
        self.assertEqual(claims.expires_at, NOW + timedelta(days=365))
        self.assertEqual(claims.tier, "pro")

    def test_tampering_and_different_serial_fail(self):
        code = sign_license(self.key, self.serial, issued_at=NOW)
        with self.assertRaises(InvalidLicense):
            verify_license(code, "b" * 32, self.key.public_key())
        payload, signature = code.split(".")
        changed = ("A" if payload[0] != "A" else "B") + payload[1:]
        with self.assertRaises(InvalidLicense):
            verify_license(changed + "." + signature, self.serial, self.key.public_key())

    def test_signed_code_for_another_product_fails(self):
        code = sign_license(self.key, self.serial, issued_at=NOW)
        encoded, _signature = code.split(".")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        payload["product"] = "another-product"
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        changed = (
            base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") + "."
            + base64.urlsafe_b64encode(self.key.sign(raw)).decode("ascii").rstrip("=")
        )
        with self.assertRaises(InvalidLicense):
            verify_license(changed, self.serial, self.key.public_key())

    def test_malformed_and_oversize_fail(self):
        for code in ("", "abc", "a.b.c", "a" * 2000):
            with self.subTest(code=code[:20]), self.assertRaises(InvalidLicense):
                verify_license(code, self.serial, self.key.public_key())

    def test_mac_issuer_rejects_key_that_the_app_cannot_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / "issuer.pem"
            private_path.write_bytes(self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            os.chmod(private_path, 0o600)
            command = [
                sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "issue-license.py"),
                "--key", str(private_path), self.serial,
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("私钥与应用公钥不匹配", result.stderr)

    def test_mac_issuer_accepts_matching_external_key(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "issue-license.py"
        spec = importlib.util.spec_from_file_location("test_license_issuer", path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            private_path = Path(directory) / "issuer.pem"
            private_path.write_bytes(self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            os.chmod(private_path, 0o600)
            with mock.patch.object(module, "production_public_key", return_value=self.key.public_key()):
                code = module.issue(private_path, self.serial)
            self.assertEqual(verify_license(code, self.serial, self.key.public_key()).tier, "pro")


class LicenseServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "database" / "app.db")
        self.db.migrate()
        self.users = UserService(self.db, PasswordHasher())
        self.users.setup_admin("admin", "administrator1", "管理员", "行政部")
        self.user = self.users.register("member", "memberpass1", "测试用户", "财务部")
        self.users.approve(1, self.user.id)
        self.key = Ed25519PrivateKey.generate()
        self.licenses = LicenseService(self.db, self.key.public_key())

    def tearDown(self):
        self.temp.cleanup()

    def test_import_expiry_and_renewal(self):
        serial = self.licenses.status(self.user.id, now=NOW).serial
        first = sign_license(self.key, serial, issued_at=NOW)
        state = self.licenses.import_code(self.user.id, first, now=NOW)
        self.assertEqual(state.tier, "pro")
        self.assertEqual(self.licenses.status(self.user.id, now=NOW + timedelta(days=364)).tier, "pro")
        self.assertEqual(self.licenses.status(self.user.id, now=NOW + timedelta(days=365)).tier, "basic")
        renewal = sign_license(self.key, serial, issued_at=NOW + timedelta(days=365))
        self.assertEqual(self.licenses.import_code(self.user.id, renewal, now=NOW + timedelta(days=365)).tier, "pro")

    def test_serial_is_unique_and_survives_profile_changes(self):
        first = self.licenses.status(self.user.id, now=NOW).serial
        other = self.users.register("second", "memberpass2", "另一用户", "财务部")
        self.users.approve(1, other.id)
        second = self.licenses.status(other.id, now=NOW).serial
        self.assertNotEqual(first, second)
        self.users.update_own_profile(self.user.id, "更新用户", "行政部")
        self.assertEqual(self.licenses.status(self.user.id, now=NOW).serial, first)

    def test_import_rejects_other_user_and_older_license(self):
        serial = self.licenses.status(self.user.id, now=NOW).serial
        wrong = sign_license(self.key, "b" * 32, issued_at=NOW)
        with self.assertRaises(InvalidLicense):
            self.licenses.import_code(self.user.id, wrong, now=NOW)
        newer = sign_license(self.key, serial, issued_at=NOW + timedelta(days=1))
        self.licenses.import_code(self.user.id, newer, now=NOW + timedelta(days=1))
        older = sign_license(self.key, serial, issued_at=NOW)
        with self.assertRaises(InvalidLicense):
            self.licenses.import_code(self.user.id, older, now=NOW + timedelta(days=1))

    def test_clock_rollback_suspends_pro(self):
        serial = self.licenses.status(self.user.id, now=NOW).serial
        code = sign_license(self.key, serial, issued_at=NOW)
        self.licenses.import_code(self.user.id, code, now=NOW)
        self.assertEqual(self.licenses.status(self.user.id, now=NOW + timedelta(days=2)).tier, "pro")
        rolled = self.licenses.status(self.user.id, now=NOW + timedelta(days=1))
        self.assertEqual(rolled.tier, "basic")
        self.assertEqual(rolled.reason, "clock_rollback")
        with self.assertRaises(ClockRollback):
            self.licenses.import_code(self.user.id, code, now=NOW + timedelta(days=1))

    def test_operator_can_clear_clock_high_water_after_correcting_server_time(self):
        serial = self.licenses.status(self.user.id, now=NOW).serial
        code = sign_license(self.key, serial, issued_at=NOW)
        self.licenses.import_code(self.user.id, code, now=NOW)
        self.licenses.status(self.user.id, now=NOW + timedelta(days=30))
        self.assertEqual(self.licenses.status(self.user.id, now=NOW + timedelta(days=1)).reason, "clock_rollback")
        self.licenses.reset_clock(serial)
        self.assertEqual(self.licenses.status(self.user.id, now=NOW + timedelta(days=1)).tier, "pro")

    def test_local_clock_recovery_command_targets_only_given_serial(self):
        serial = self.licenses.status(self.user.id, now=NOW).serial
        other = self.users.register("second", "memberpass2", "另一用户", "财务部")
        self.users.approve(1, other.id)
        other_serial = self.licenses.status(other.id, now=NOW).serial
        self.licenses.status(self.user.id, now=NOW + timedelta(days=30))
        self.licenses.status(other.id, now=NOW + timedelta(days=30))
        data_dir = Path(self.temp.name)
        command = [
            sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "reset-license-clock.py"),
            serial, "--data-dir", str(data_dir),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.licenses.status(self.user.id, now=NOW + timedelta(days=1)).reason)
        self.assertEqual(self.licenses.status(other.id, now=NOW + timedelta(days=1)).reason, "clock_rollback")
