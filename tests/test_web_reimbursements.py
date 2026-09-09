from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import quote

from reimbursements import (
    OwnedReimbursementFile,
    ReimbursementGenerationError,
    ReimbursementRecord,
    ReimbursementStorageError,
)
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


class PartialReadFailureStream(TrackingStream):
    def read(self, size=-1):
        if self.tell() >= 65536:
            self.read_sizes.append(size)
            raise OSError("private read failure")
        return super().read(size)


class CloseFailureStream(TrackingStream):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.close_attempts = 0

    def close(self):
        self.close_attempts += 1
        super().close()
        raise OSError("private close failure")


class PartialRuntimeFailureStream(TrackingStream):
    def read(self, size=-1):
        if self.tell() >= 65536:
            self.read_sizes.append(size)
            raise RuntimeError("private runtime read failure")
        return super().read(size)


class RuntimeCloseFailureStream(TrackingStream):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.close_attempts = 0

    def close(self):
        self.close_attempts += 1
        super().close()
        raise RuntimeError("private runtime close failure")


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

    def _post_generation(self):
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
            ("payload", json.dumps(payload), None, "application/json"),
            ("screenshots", b"PNGDATA", "route.png", "image/png"),
        ])
        return self.alice.request(
            "POST",
            "/api/reimbursements/generate",
            body=body,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "X-CSRF-Token": self.alice.csrf_for("/api/reimbursements/generate"),
            },
        )

    def _insert_record(
        self,
        user_id: int,
        record_id: str,
        *,
        created_at: str | None = None,
        deleted_at: str | None = None,
        xlsx_name: str = "报销 明细.xlsx",
        pdf_name: str = "报销 明细.pdf",
        xlsx_bytes: bytes = b"xlsx",
        pdf_bytes: bytes = b"pdf",
        reason: str | None = None,
        reimbursement_amount: str | None = None,
    ) -> ReimbursementRecord:
        record_dir = self.running.data_dir / "users" / str(user_id) / record_id
        record_dir.mkdir(parents=True)
        (record_dir / xlsx_name).write_bytes(xlsx_bytes)
        (record_dir / pdf_name).write_bytes(pdf_bytes)
        created_at = created_at or "2026-09-08T00:00:00+00:00"
        record = ReimbursementRecord(
            id=record_id,
            user_id=user_id,
            reimbursement_date="2026-09-08",
            display_name=xlsx_name,
            xlsx_path=f"users/{user_id}/{record_id}/{xlsx_name}",
            pdf_path=f"users/{user_id}/{record_id}/{pdf_name}",
            created_at=created_at,
            deleted_at=deleted_at,
            reason=reason,
            reimbursement_amount=reimbursement_amount,
        )
        with self.running.server.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO reimbursements(
                    id, user_id, reimbursement_date, display_name,
                    xlsx_path, pdf_path, created_at, deleted_at,
                    reason, reimbursement_amount
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.user_id,
                    record.reimbursement_date,
                    record.display_name,
                    record.xlsx_path,
                    record.pdf_path,
                    record.created_at,
                    record.deleted_at,
                    record.reason,
                    record.reimbursement_amount,
                ),
            )
        return record

    def test_stats_endpoint_is_authenticated_and_returns_active_owner_totals(self):
        service = mock.Mock()
        service.active_stats.return_value = {
            "month_count": 2,
            "year_count": 5,
            "year_amount": "1234.50",
        }
        application = self.running.server.application
        with mock.patch.object(application, "reimbursement_service", service):
            response = self.alice.get("/api/reimbursements/stats")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), service.active_stats.return_value)
        service.active_stats.assert_called_once_with(self.alice_id)
        self.assertEqual(self.admin.get("/api/reimbursements/stats").status, 403)
        self.assertEqual(self.anonymous.get("/api/reimbursements/stats").status, 401)

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

    def test_partial_download_read_failure_commits_only_one_real_response(self):
        record_id = "30000000-0000-4000-8000-000000000004"
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
        stream = PartialReadFailureStream(b"a" * 65537)
        owned = OwnedReimbursementFile(record, "xlsx", "claim.xlsx", 65537, stream)
        service = mock.Mock()
        service.owned_file.return_value = owned
        cookie = "; ".join(
            f"{item.name}={item.value}"
            for item in self.alice.cookies
            if not item.secure
        )
        request = (
            f"GET /api/reimbursements/{record_id}/xlsx HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\nCookie: {cookie}\r\n\r\n"
        ).encode("ascii")
        connection = socket.create_connection(
            self.running.server.server_address,
            timeout=2,
        )
        try:
            with mock.patch.object(
                self.running.server.application,
                "reimbursement_service",
                service,
            ), self.assertNoLogs("web", level="ERROR"):
                connection.sendall(request)
                chunks = []
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
        finally:
            connection.close()

        response = b"".join(chunks)
        head, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b" 200 ", head.split(b"\r\n", 1)[0])
        self.assertNotIn(b"\r\nHTTP/", body)
        self.assertEqual(body, b"a" * 65536)
        self.assertEqual(stream.read_sizes, [65536, 65536])
        self.assertTrue(stream.closed)

    def test_download_close_failure_after_body_does_not_emit_second_response(self):
        record_id = "30000000-0000-4000-8000-000000000005"
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
        stream = CloseFailureStream(b"body")
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
        service = mock.Mock()
        service.owned_file.return_value = owned
        user = SimpleNamespace(
            user_id=self.alice_id,
            role="user",
            must_change_password=False,
        )

        with mock.patch.object(
            application.session_service,
            "resolve",
            return_value=user,
        ), mock.patch.object(
            application,
            "reimbursement_service",
            service,
        ), self.assertNoLogs("web", level="ERROR"):
            application.handle_get(handler)

        self.assertEqual(handler.send_response.call_args_list, [mock.call(200)])
        handler.wfile.write.assert_called_once_with(b"body")
        self.assertEqual(stream.close_attempts, 1)
        self.assertTrue(stream.closed)
        self.assertTrue(handler.close_connection)

    def test_partial_download_runtime_failure_does_not_emit_second_response(self):
        record_id = "30000000-0000-4000-8000-000000000006"
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
        stream = PartialRuntimeFailureStream(b"a" * 65537)
        owned = OwnedReimbursementFile(record, "xlsx", "claim.xlsx", 65537, stream)
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
        service = mock.Mock()
        service.owned_file.return_value = owned
        user = SimpleNamespace(
            user_id=self.alice_id,
            role="user",
            must_change_password=False,
        )

        with mock.patch.object(
            application.session_service,
            "resolve",
            return_value=user,
        ), mock.patch.object(
            application,
            "reimbursement_service",
            service,
        ), self.assertNoLogs("web", level="ERROR"):
            application.handle_get(handler)

        self.assertEqual(handler.send_response.call_args_list, [mock.call(200)])
        self.assertEqual(
            handler.wfile.write.call_args_list,
            [mock.call(b"a" * 65536)],
        )
        self.assertEqual(stream.read_sizes, [65536, 65536])
        self.assertTrue(stream.closed)
        self.assertTrue(handler.close_connection)

    def test_download_runtime_close_failure_does_not_emit_second_response(self):
        record_id = "30000000-0000-4000-8000-000000000007"
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
        stream = RuntimeCloseFailureStream(b"body")
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
        service = mock.Mock()
        service.owned_file.return_value = owned
        user = SimpleNamespace(
            user_id=self.alice_id,
            role="user",
            must_change_password=False,
        )

        with mock.patch.object(
            application.session_service,
            "resolve",
            return_value=user,
        ), mock.patch.object(
            application,
            "reimbursement_service",
            service,
        ), self.assertNoLogs("web", level="ERROR"):
            application.handle_get(handler)

        self.assertEqual(handler.send_response.call_args_list, [mock.call(200)])
        handler.wfile.write.assert_called_once_with(b"body")
        self.assertEqual(stream.close_attempts, 1)
        self.assertTrue(stream.closed)
        self.assertTrue(handler.close_connection)

    def test_active_and_trash_lists_are_isolated_and_do_not_expose_paths(self):
        active = self._insert_record(
            self.alice_id,
            "40000000-0000-4000-8000-000000000001",
            reason="客户拜访",
            reimbursement_amount="101.00",
        )
        deleted_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        trashed = self._insert_record(
            self.alice_id,
            "40000000-0000-4000-8000-000000000002",
            deleted_at=deleted_at,
            reason="现场服务",
            reimbursement_amount="220.50",
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
        active_payload = active_response.json()["reimbursements"][0]
        self.assertEqual(active_payload["reason"], "客户拜访")
        self.assertEqual(active_payload["reimbursement_amount"], "101.00")
        trash_payload = trash_response.json()["reimbursements"]
        self.assertEqual([item["id"] for item in trash_payload], [trashed.id])
        self.assertEqual(trash_payload[0]["reason"], "现场服务")
        self.assertEqual(trash_payload[0]["reimbursement_amount"], "220.50")
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

    def test_purge_storage_failure_returns_stable_retryable_error(self):
        record_id = "50000000-0000-4000-8000-000000000002"
        path = f"/api/reimbursements/{record_id}/purge"
        service = self.running.server.application.reimbursement_service

        with mock.patch.object(
            service,
            "purge_one",
            side_effect=ReimbursementStorageError("private storage detail"),
        ):
            response = self.alice.post_json(path, {"confirm": True})

        self.assertEqual(response.status, 503)
        self.assertEqual(
            response.json(),
            {"error": "报销记录暂时无法删除，请稍后重试"},
        )
        self.assertNotIn("private", response.text)

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

    def test_unsupported_methods_only_advertise_canonical_reimbursement_routes(self):
        valid = "60000000-0000-4000-8000-000000000001"
        invalid_paths = (
            f"/api/reimbursements/{valid}/zip",
            "/api/reimbursements/not-a-uuid/pdf",
            f"/api/reimbursements/{valid}/pdf/extra",
        )
        for method in ("HEAD", "PUT"):
            for path in invalid_paths:
                with self.subTest(method=method, path=path):
                    response = self.alice.raw_request(method, path)
                    self.assertEqual(response.status, 404)
                    self.assertIsNone(response.headers.get("Allow"))

        valid_cases = (
            ("HEAD", f"/api/reimbursements/{valid}/pdf", "GET"),
            ("PUT", f"/api/reimbursements/{valid}/purge", "POST"),
        )
        for method, path, allow in valid_cases:
            with self.subTest(method=method, path=path):
                response = self.alice.raw_request(method, path)
                self.assertEqual(response.status, 405)
                self.assertEqual(response.headers["Allow"], allow)

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
            "客户拜访",
            "101.00",
        )
        captured = {}

        def generate(user, raw_payload, screenshots):
            captured["user"] = user
            captured["payload"] = raw_payload
            captured["upload_directory"] = screenshots[0].parent
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
        self.assertEqual(generated["record"]["reason"], "客户拜访")
        self.assertEqual(generated["record"]["reimbursement_amount"], "101.00")
        self.assertEqual(captured["user"].user_id, self.alice_id)
        self.assertEqual(captured["payload"]["traveler"], "client traveler")
        self.assertEqual(captured["screenshots"], [("0000-route.png", b"PNGDATA", True)])
        self.assertFalse(captured["upload_directory"].exists())

    def test_generation_returns_committed_record_when_upload_cleanup_fails(self):
        service = self.running.server.application.reimbursement_service
        temporary = tempfile.TemporaryDirectory(dir=self.running.root)
        self.addCleanup(temporary.cleanup)
        captured_screenshots = []

        def generate(_template, work_dir, _payload, screenshots):
            captured_screenshots.extend(path.read_bytes() for path in screenshots)
            path = work_dir / "生成结果.xlsx"
            path.write_bytes(b"xlsx")
            return SimpleNamespace(path=path)

        def export(_xlsx, work_dir, _soffice):
            path = work_dir / "生成结果.pdf"
            path.write_bytes(b"pdf")
            return path

        with mock.patch.object(service, "_generator", side_effect=generate), \
            mock.patch.object(service, "_pdf_exporter", side_effect=export), \
            mock.patch.object(service, "_soffice_finder", return_value="/fake/soffice"), \
            mock.patch("web.tempfile.TemporaryDirectory", return_value=temporary), \
            mock.patch.object(
                temporary, "cleanup", side_effect=OSError("private upload cleanup failure")
            ) as cleanup, self.assertLogs("web", level="INFO") as logs:
            response = self._post_generation()

        records = service.list_active(self.alice_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(captured_screenshots, [b"PNGDATA"])
        self.assertEqual(response.status, 200)
        generated = response.json()
        self.assertEqual(generated["record"]["id"], records[0].id)
        self.assertEqual(self.alice.get(generated["xlsx_url"]).body, b"xlsx")
        self.assertEqual(self.alice.get(generated["pdf_url"]).body, b"pdf")
        cleanup.assert_called_once_with()
        cleanup_logs = [
            record for record in logs.records
            if "reimbursement upload cleanup failed" in record.getMessage()
        ]
        self.assertEqual(len(cleanup_logs), 1)
        self.assertIn("exception=OSError", cleanup_logs[0].getMessage())
        self.assertNotIn("private upload cleanup failure", "\n".join(logs.output))
        self.assertIsNone(cleanup_logs[0].exc_info)

    def test_generation_failure_survives_upload_cleanup_failure(self):
        service = self.running.server.application.reimbursement_service
        temporary = tempfile.TemporaryDirectory(dir=self.running.root)
        self.addCleanup(temporary.cleanup)

        with mock.patch.object(
            service, "_generator", side_effect=ValueError("private generator failure")
        ), mock.patch("web.tempfile.TemporaryDirectory", return_value=temporary), \
            mock.patch.object(
                temporary, "cleanup", side_effect=OSError("private upload cleanup failure")
            ) as cleanup, self.assertLogs(level="INFO") as logs:
            response = self._post_generation()

        self.assertEqual(response.status, 500)
        self.assertEqual(response.json(), {"error": "生成报销文件失败，请稍后重试"})
        self.assertEqual(service.list_active(self.alice_id), [])
        self.assertEqual(list((self.running.data_dir / "tmp").iterdir()), [])
        cleanup.assert_called_once_with()
        messages = "\n".join(logs.output)
        self.assertIn("exception=ValueError", messages)
        self.assertIn("exception=OSError", messages)
        self.assertNotIn("private generator failure", messages)
        self.assertNotIn("private upload cleanup failure", messages)
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    def test_upload_write_failure_survives_upload_cleanup_failure(self):
        service = self.running.server.application.reimbursement_service
        temporary = tempfile.TemporaryDirectory(dir=self.running.root)
        self.addCleanup(temporary.cleanup)
        upload_error = OSError("private upload write failure")
        original_write_bytes = Path.write_bytes

        def write_bytes(path, data):
            if path.parent == Path(temporary.name):
                raise upload_error
            return original_write_bytes(path, data)

        with mock.patch.object(Path, "write_bytes", write_bytes), \
            mock.patch.object(service, "generate", wraps=service.generate) as generate, \
            mock.patch("web.tempfile.TemporaryDirectory", return_value=temporary), \
            mock.patch.object(
                temporary, "cleanup", side_effect=RuntimeError("private upload cleanup failure")
            ) as cleanup, self.assertLogs("web", level="ERROR") as logs:
            response = self._post_generation()

        self.assertEqual(response.status, 500)
        self.assertEqual(response.json(), {"error": "服务暂时不可用，请稍后重试"})
        self.assertEqual(service.list_active(self.alice_id), [])
        generate.assert_not_called()
        cleanup.assert_called_once_with()
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        self.assertNotIn("private upload write failure", "\n".join(logs.output))
        self.assertIn("exception=RuntimeError", "\n".join(logs.output))
        self.assertNotIn("private upload cleanup failure", "\n".join(logs.output))

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

    def test_upload_admission_bounds_body_buffering_to_generation_capacity(self):
        application = self.running.server.application
        capacity = application.config.max_concurrent_generations
        request_count = capacity + 2
        start = threading.Barrier(request_count + 1)
        admitted = threading.Event()
        overflow = threading.Event()
        release = threading.Event()
        count_lock = threading.Lock()
        active = 0
        peak = 0
        user = SimpleNamespace(user_id=self.alice_id, role="user")

        def block_body_buffering(_handler):
            nonlocal active, peak
            with count_lock:
                active += 1
                peak = max(peak, active)
                if active >= capacity:
                    admitted.set()
                if active > capacity:
                    overflow.set()
            try:
                if not release.wait(timeout=2):
                    raise TimeoutError("test did not release body buffering")
                return None
            finally:
                with count_lock:
                    active -= 1

        def upload():
            start.wait(timeout=1)
            application._reimbursement_generate(SimpleNamespace(), user, None)

        threads = [threading.Thread(target=upload) for _ in range(request_count)]
        with mock.patch.object(
            application,
            "_multipart_body",
            side_effect=block_body_buffering,
        ):
            for thread in threads:
                thread.start()
            start.wait(timeout=1)
            self.assertTrue(admitted.wait(timeout=1))
            try:
                self.assertFalse(
                    overflow.wait(timeout=0.1),
                    "too many uploads reached request-body buffering",
                )
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(peak, capacity)

    def test_upload_admission_releases_slots_after_parse_and_generation_failures(self):
        application = self.running.server.application
        slots = getattr(application, "_upload_slots", None)
        self.assertIsNotNone(slots)
        user = SimpleNamespace(
            user_id=self.alice_id,
            role="user",
            real_name="张三",
            department="技术部",
        )

        def handler_for(content_type):
            headers = Message()
            headers["Content-Type"] = content_type
            return SimpleNamespace(
                headers=headers,
                command="POST",
                send_response=mock.Mock(),
                send_header=mock.Mock(),
                end_headers=mock.Mock(),
                wfile=mock.Mock(),
            )

        invalid_handler = handler_for("multipart/form-data; boundary=invalid")
        with mock.patch.object(
            application,
            "_multipart_body",
            return_value=b"not multipart",
        ):
            application._reimbursement_generate(invalid_handler, user, None)
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

        payload = {
            "date": "2026-09-08",
            "department": "ignored",
            "traveler": "ignored",
            "reason": "客户拜访",
            "days": 1,
            "allowance": 50,
            "rows": [],
        }
        body, content_type = multipart(
            [("payload", json.dumps(payload), None, "application/json")]
        )
        failing_handler = handler_for(content_type)
        service = mock.Mock()
        service.generate.side_effect = ReimbursementGenerationError(
            "生成报销文件失败，请稍后重试"
        )
        with mock.patch.object(
            application,
            "_multipart_body",
            return_value=body,
        ), mock.patch.object(application, "reimbursement_service", service):
            application._reimbursement_generate(failing_handler, user, None)
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()


if __name__ == "__main__":
    unittest.main()
