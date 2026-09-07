from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import quote

from reimbursements import OwnedReimbursementFile, ReimbursementRecord
from tests.http_helpers import RunningApp


ADMIN_PASSWORD = "administrator1"
USER_PASSWORD = "correct horse battery staple"


def multipart(parts, boundary="reimbursement-test"):
    body = bytearray()
    for name, value, filename, content_type in parts:
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body.extend(f"Content-Disposition: {disposition}\r\n".encode("utf-8"))
        if content_type:
            body.extend(f"Content-Type: {content_type}\r\n".encode("ascii"))
        body.extend(b"\r\n")
        body.extend(value if isinstance(value, bytes) else value.encode("utf-8"))
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class TrackingStream(io.BytesIO):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class WebReimbursementTest(unittest.TestCase):
    def setUp(self):
        self.running = RunningApp().__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        self.running.client.post_json(
            "/api/setup",
            {
                "username": "admin",
                "password": ADMIN_PASSWORD,
                "real_name": "管理员",
                "department": "管理部",
            },
        )
        self.admin = self.running.new_client()
        self.assertEqual(
            self.admin.post_json(
                "/api/login",
                {"username": "admin", "password": ADMIN_PASSWORD},
            ).status,
            200,
        )
        for username, real_name in (("alice", "张三"), ("bob", "李四")):
            registering = self.running.new_client()
            self.assertEqual(
                registering.post_json(
                    "/api/register",
                    {
                        "username": username,
                        "password": USER_PASSWORD,
                        "real_name": real_name,
                        "department": "技术部",
                    },
                ).status,
                201,
            )
        with self.running.server.database.connect() as connection:
            rows = connection.execute("SELECT id, username FROM users").fetchall()
        ids = {row["username"]: row["id"] for row in rows}
        self.admin_id = ids["admin"]
        self.alice_id = ids["alice"]
        self.bob_id = ids["bob"]
        self.running.users.approve(self.admin_id, self.alice_id)
        self.running.users.approve(self.admin_id, self.bob_id)
        self.alice = self._login("alice")
        self.bob = self._login("bob")
        self.anonymous = self.running.new_client()

    def _login(self, username: str):
        client = self.running.new_client()
        response = client.post_json(
            "/api/login",
            {"username": username, "password": USER_PASSWORD},
        )
        self.assertEqual(response.status, 200)
        return client

    def _insert_record(
        self,
        user_id: int,
        record_id: str,
        *,
        deleted_at: str | None = None,
        xlsx_name: str = "报销 明细.xlsx",
        pdf_name: str = "报销 明细.pdf",
        xlsx_bytes: bytes = b"xlsx",
        pdf_bytes: bytes = b"pdf",
    ) -> ReimbursementRecord:
        record_dir = self.running.data_dir / "users" / str(user_id) / record_id
        record_dir.mkdir(parents=True)
        (record_dir / xlsx_name).write_bytes(xlsx_bytes)
        (record_dir / pdf_name).write_bytes(pdf_bytes)
        created_at = "2026-09-08T00:00:00+00:00"
        record = ReimbursementRecord(
            id=record_id,
            user_id=user_id,
            reimbursement_date="2026-09-08",
            display_name=xlsx_name,
            xlsx_path=f"users/{user_id}/{record_id}/{xlsx_name}",
            pdf_path=f"users/{user_id}/{record_id}/{pdf_name}",
            created_at=created_at,
            deleted_at=deleted_at,
        )
        with self.running.server.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO reimbursements(
                    id, user_id, reimbursement_date, display_name,
                    xlsx_path, pdf_path, created_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.user_id,
                    record.reimbursement_date,
                    record.display_name,
                    record.xlsx_path,
                    record.pdf_path,
                    record.created_at,
                    record.deleted_at,
                ),
            )
        return record

    def test_file_permission_matrix_and_download_metadata(self):
        record = self._insert_record(
            self.alice_id,
            "30000000-0000-4000-8000-000000000001",
            xlsx_bytes=b"x" * 65537,
        )

        xlsx = self.alice.get(f"/api/reimbursements/{record.id}/xlsx")
        pdf = self.alice.get(f"/api/reimbursements/{record.id}/pdf")

        self.assertEqual(xlsx.status, 200)
        self.assertEqual(xlsx.body, b"x" * 65537)
        self.assertEqual(
            xlsx.headers["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual(xlsx.headers["Content-Length"], "65537")
        self.assertEqual(xlsx.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(xlsx.headers["Cache-Control"], "no-store")
        self.assertEqual(
            xlsx.headers["Content-Disposition"],
            "attachment; filename*=UTF-8''" + quote("报销 明细.xlsx", safe=""),
        )
        self.assertEqual(pdf.status, 200)
        self.assertEqual(pdf.body, b"pdf")
        self.assertEqual(pdf.headers["Content-Type"], "application/pdf")
        self.assertEqual(
            pdf.headers["Content-Disposition"],
            "inline; filename*=UTF-8''" + quote("报销 明细.pdf", safe=""),
        )
        hidden = [
            self.bob.get(f"/api/reimbursements/{record.id}/pdf"),
            self.admin.get(f"/api/reimbursements/{record.id}/pdf"),
            self.alice.get(
                "/api/reimbursements/30000000-0000-4000-8000-000000000099/pdf"
            ),
        ]
        self.assertEqual([(item.status, item.json()) for item in hidden], [
            (404, {"error": "报销记录不存在"}),
            (404, {"error": "报销记录不存在"}),
            (404, {"error": "报销记录不存在"}),
        ])
        self.assertEqual(
            self.anonymous.get(f"/api/reimbursements/{record.id}/pdf").status,
            401,
        )

    def test_download_streams_owned_descriptor_in_65536_byte_chunks_and_closes_it(self):
        record_id = "30000000-0000-4000-8000-000000000002"
        poison_record = ReimbursementRecord(
            id=record_id,
            user_id=self.alice_id,
            reimbursement_date=None,
            display_name="wrong.xlsx",
            xlsx_path="../../must-not-open.xlsx",
            pdf_path="../../must-not-open.pdf",
            created_at="2026-09-08T00:00:00+00:00",
            deleted_at=None,
        )
        stream = TrackingStream(b"a" * 65537)
        owned = OwnedReimbursementFile(
            record=poison_record,
            kind="xlsx",
            display_name="元数据.xlsx",
            size=65537,
            stream=stream,
        )
        service = mock.Mock()
        service.owned_file.return_value = owned
        application = self.running.server.application

        with mock.patch.object(application, "reimbursement_service", service):
            response = self.alice.get(f"/api/reimbursements/{record_id}/xlsx")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"a" * 65537)
        self.assertEqual(stream.read_sizes, [65536, 65536, 65536])
        self.assertTrue(stream.closed)
        self.assertIn(quote("元数据.xlsx", safe=""), response.headers["Content-Disposition"])
        self.assertNotIn("wrong", response.headers["Content-Disposition"])
        service.owned_file.assert_called_once_with(self.alice_id, record_id, "xlsx")

    def test_download_disconnect_closes_owned_stream_without_error_log(self):
        record_id = "30000000-0000-4000-8000-000000000003"
        record = ReimbursementRecord(
            record_id,
            self.alice_id,
            None,
            "claim.xlsx",
            "poison",
            "poison",
            "2026-09-08T00:00:00+00:00",
            None,
        )
        stream = TrackingStream(b"body")
        owned = OwnedReimbursementFile(record, "xlsx", "claim.xlsx", 4, stream)
        application = self.running.server.application
        headers = Message()
        headers["Cookie"] = "reimbursement_session=fake"
        handler = SimpleNamespace(
            path=f"/api/reimbursements/{record_id}/xlsx",
            command="GET",
            headers=headers,
            client_address=("127.0.0.1", 1),
            send_response=mock.Mock(),
            send_header=mock.Mock(),
            end_headers=mock.Mock(),
            wfile=mock.Mock(),
            close_connection=False,
        )
        handler.wfile.write.side_effect = ConnectionResetError("client left")
        service = mock.Mock()
        service.owned_file.return_value = owned
        user = SimpleNamespace(
            user_id=self.alice_id,
            role="user",
            must_change_password=False,
        )

        with mock.patch.object(application.session_service, "resolve", return_value=user), \
             mock.patch.object(application, "reimbursement_service", service), \
             self.assertNoLogs("web", level="ERROR"):
            application.handle_get(handler)

        self.assertTrue(stream.closed)
        self.assertTrue(handler.close_connection)

    def test_active_and_trash_lists_are_isolated_and_do_not_expose_paths(self):
        active = self._insert_record(
            self.alice_id, "40000000-0000-4000-8000-000000000001"
        )
        deleted_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        trashed = self._insert_record(
            self.alice_id,
            "40000000-0000-4000-8000-000000000002",
            deleted_at=deleted_at,
        )
        self._insert_record(
            self.bob_id, "40000000-0000-4000-8000-000000000003"
        )

        active_response = self.alice.get("/api/reimbursements?scope=active")
        trash_response = self.alice.get("/api/reimbursements?scope=trash")

        self.assertEqual(active_response.status, 200)
        self.assertEqual(
            [item["id"] for item in active_response.json()["reimbursements"]],
            [active.id],
        )
        trash_payload = trash_response.json()["reimbursements"]
        self.assertEqual([item["id"] for item in trash_payload], [trashed.id])
        self.assertEqual(
            trash_payload[0]["purge_at"],
            (datetime.fromisoformat(deleted_at) + timedelta(days=30)).isoformat(),
        )
        serialized = active_response.text + trash_response.text
        self.assertNotIn("xlsx_path", serialized)
        self.assertNotIn("pdf_path", serialized)
        self.assertNotIn(f"users/{self.alice_id}", serialized)
        self.assertEqual(self.admin.get("/api/reimbursements?scope=active").status, 403)
        self.assertEqual(self.anonymous.get("/api/reimbursements?scope=active").status, 401)
        self.assertEqual(self.alice.get("/api/reimbursements?scope=unknown").status, 400)

    def test_trash_restore_and_exact_confirmation_purge_require_csrf(self):
        record = self._insert_record(
            self.alice_id, "50000000-0000-4000-8000-000000000001"
        )
        trash_path = f"/api/reimbursements/{record.id}/trash"
        restore_path = f"/api/reimbursements/{record.id}/restore"
        purge_path = f"/api/reimbursements/{record.id}/purge"

        self.assertEqual(self.admin.post_json(trash_path, {}).status, 403)
        self.assertEqual(self.alice.post_json(trash_path, {}, csrf=False).status, 403)
        self.assertEqual(self.alice.post_json(trash_path, {}).status, 200)
        self.assertEqual(self.alice.get(f"/api/reimbursements/{record.id}/xlsx").status, 404)
        self.assertEqual(self.bob.post_json(restore_path, {}).status, 404)
        self.assertEqual(self.alice.post_json(restore_path, {}, csrf=False).status, 403)
        self.assertEqual(self.alice.post_json(restore_path, {}).status, 200)
        self.assertEqual(self.alice.get(f"/api/reimbursements/{record.id}/xlsx").status, 200)
        self.assertEqual(self.alice.post_json(trash_path, {}).status, 200)
        for payload in ({}, {"confirm": False}, {"confirm": 1}, {"confirm": "true"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.alice.post_json(purge_path, payload).status, 400)
        self.assertEqual(self.alice.post_json(purge_path, {"confirm": True}, csrf=False).status, 403)
        self.assertEqual(self.alice.post_json(purge_path, {"confirm": True}).status, 200)
        self.assertFalse((self.running.data_dir / record.xlsx_path).exists())
        with self.running.server.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM reimbursements WHERE id = ?", (record.id,)
            ).fetchone()
        self.assertIsNone(row)

    def test_invalid_record_kinds_uuids_and_path_shapes_are_stable_404(self):
        valid = "60000000-0000-4000-8000-000000000001"
        paths = (
            f"/api/reimbursements/{valid}/zip",
            "/api/reimbursements/not-a-uuid/pdf",
            f"/api/reimbursements/{valid}/pdf/extra",
            f"/api/reimbursements/{valid}%2Fpdf",
            f"/api/reimbursements/{valid.upper()}/pdf",
            "/api/reimbursements/../pdf",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.alice.get(path)
                self.assertEqual(response.status, 404)
                self.assertEqual(set(response.json()), {"error"})

    def test_replaced_session_is_denied_before_history_service(self):
        old_alice = self.alice
        replacement = self._login("alice")
        service = mock.Mock()
        service.list_active.return_value = []
        application = self.running.server.application

        with mock.patch.object(application, "reimbursement_service", service):
            stale = old_alice.get("/api/reimbursements?scope=active")
            current = replacement.get("/api/reimbursements?scope=active")

        self.assertEqual(stale.status, 401)
        self.assertIn("Max-Age=0", stale.headers["Set-Cookie"])
        self.assertEqual(current.status, 200)
        service.list_active.assert_called_once_with(self.alice_id)

    def test_generation_route_preserves_multipart_screenshots_and_returns_downloads(self):
        payload = {
            "date": "2026-09-08",
            "department": "client department",
            "traveler": "client traveler",
            "reason": "客户拜访",
            "days": 2,
            "allowance": 50,
            "rows": [],
        }
        body, content_type = multipart([
            ("payload", json.dumps(payload, ensure_ascii=False), None, "application/json"),
            ("screenshots", b"PNGDATA", "route.png", "image/png"),
        ])
        record_id = "70000000-0000-4000-8000-000000000001"
        record = ReimbursementRecord(
            record_id,
            self.alice_id,
            "2026-09-08",
            "生成结果.xlsx",
            f"users/{self.alice_id}/{record_id}/生成结果.xlsx",
            f"users/{self.alice_id}/{record_id}/生成结果.pdf",
            "2026-09-08T00:00:00+00:00",
            None,
        )
        captured = {}

        def generate(user, raw_payload, screenshots):
            captured["user"] = user
            captured["payload"] = raw_payload
            captured["screenshots"] = [
                (path.name, path.read_bytes(), path.is_file()) for path in screenshots
            ]
            return record

        service = mock.Mock()
        service.generate.side_effect = generate
        application = self.running.server.application
        csrf = self.alice.csrf_for("/api/reimbursements/generate")
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "X-CSRF-Token": csrf,
        }

        with mock.patch.object(application, "reimbursement_service", service):
            response = self.alice.request(
                "POST",
                "/api/reimbursements/generate",
                body=body,
                headers=headers,
            )

        self.assertEqual(response.status, 200)
        generated = response.json()
        self.assertEqual(generated["record"]["id"], record_id)
        self.assertEqual(generated["xlsx_url"], f"/api/reimbursements/{record_id}/xlsx")
        self.assertEqual(generated["pdf_url"], f"/api/reimbursements/{record_id}/pdf")
        self.assertEqual(generated["xlsx_filename"], "生成结果.xlsx")
        self.assertEqual(generated["pdf_filename"], "生成结果.pdf")
        self.assertEqual(captured["user"].user_id, self.alice_id)
        self.assertEqual(captured["payload"]["traveler"], "client traveler")
        self.assertEqual(captured["screenshots"], [("0000-route.png", b"PNGDATA", True)])

    def test_generation_requires_user_role_current_session_and_csrf_before_parsing(self):
        service = mock.Mock()
        application = self.running.server.application
        body, content_type = multipart([
            ("payload", b"{}", None, "application/json"),
        ])
        headers = {"Content-Type": content_type, "Content-Length": str(len(body))}

        with mock.patch.object(application, "reimbursement_service", service):
            self.assertEqual(
                self.alice.request(
                    "POST", "/api/reimbursements/generate", body=body, headers=headers
                ).status,
                403,
            )
            self.assertEqual(
                self.admin.request(
                    "POST", "/api/reimbursements/generate", body=body, headers=headers
                ).status,
                403,
            )
            self.assertEqual(
                self.anonymous.request(
                    "POST", "/api/reimbursements/generate", body=body, headers=headers
                ).status,
                401,
            )
        service.generate.assert_not_called()

    def test_generation_enforces_multipart_body_limit_before_service(self):
        body, content_type = multipart([
            ("payload", b"{}", None, "application/json"),
        ])
        service = mock.Mock()
        application = self.running.server.application
        csrf = self.alice.csrf_for("/api/reimbursements/generate")
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "X-CSRF-Token": csrf,
        }
        original_limit = application.config.max_body_bytes
        object.__setattr__(application.config, "max_body_bytes", len(body) - 1)
        try:
            with mock.patch.object(application, "reimbursement_service", service):
                response = self.alice.request(
                    "POST",
                    "/api/reimbursements/generate",
                    body=body,
                    headers=headers,
                )
        finally:
            object.__setattr__(application.config, "max_body_bytes", original_limit)

        self.assertEqual(response.status, 413)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        service.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
