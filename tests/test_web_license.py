from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from datetime import datetime, timedelta, timezone
import unittest

from license import sign_license
from tests.http_helpers import RunningApp


class LicenseHttpTest(unittest.TestCase):
    def test_profile_offers_serial_and_license_import(self):
        with RunningApp() as running:
            admin = running.users.setup_admin("admin", "administrator1", "管理员", "行政部")
            pending = running.users.register("alice", "memberpass1", "测试用户", "财务部")
            running.users.approve(admin.id, pending.id)
            client = running.new_client()
            client.post_json("/api/login", {"username": "alice", "password": "memberpass1"})
            page = client.get("/profile")
            self.assertEqual(page.status, 200)
            self.assertIn('id="license-serial"', page.text)
            self.assertIn('id="license-import-form"', page.text)
            self.assertEqual(client.get("/static/pro-upgrade-payment-qr.jpg").status, 200)

    def test_user_can_import_only_own_signed_code_with_csrf(self):
        with RunningApp() as running:
            admin = running.users.setup_admin("admin", "administrator1", "管理员", "行政部")
            pending = running.users.register("alice", "memberpass1", "测试用户", "财务部")
            running.users.approve(admin.id, pending.id)
            client = running.new_client()
            self.assertEqual(client.get("/api/license").status, 401)
            self.assertEqual(client.post_json("/api/login", {
                "username": "alice", "password": "memberpass1"
            }).status, 200)
            before = client.get("/api/license")
            self.assertEqual(before.status, 200, before.text)
            self.assertEqual(before.json()["tier"], "basic")
            self.assertEqual(client.get("/api/permissions").json()["invoice_limit"], 6)
            self.assertFalse(client.get("/api/permissions").json()["reimbursement_stats"])
            self.assertFalse(client.get("/api/permissions").json()["travel_trend"])
            serial = before.json()["serial"]
            key = Ed25519PrivateKey.generate()
            running.server.application.license_service._public_key = key.public_key()
            code = sign_license(key, serial)
            self.assertEqual(client.post_json(
                "/api/license/import", {"code": code}, csrf=False
            ).status, 403)
            self.assertEqual(client.post_json(
                "/api/license/import", {"code": sign_license(key, "b" * 32)}
            ).status, 400)
            changed = ("A" if code[0] != "A" else "B") + code[1:]
            self.assertEqual(client.post_json(
                "/api/license/import", {"code": changed}
            ).status, 400)
            expired = sign_license(
                key, serial, issued_at=datetime.now(timezone.utc) - timedelta(days=366)
            )
            self.assertEqual(client.post_json(
                "/api/license/import", {"code": expired}
            ).status, 400)
            accepted = client.post_json("/api/license/import", {"code": code})
            self.assertEqual(accepted.status, 200, accepted.text)
            self.assertEqual(accepted.json()["tier"], "pro")
            self.assertEqual(client.get("/api/license").json()["tier"], "pro")
            self.assertIsNone(client.get("/api/permissions").json()["invoice_limit"])
            self.assertTrue(client.get("/api/permissions").json()["reimbursement_stats"])
            self.assertTrue(client.get("/api/permissions").json()["travel_trend"])
