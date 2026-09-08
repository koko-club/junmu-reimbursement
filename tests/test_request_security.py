from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import logging
import secrets
import socket
import threading
import unittest
from unittest import mock

import app
from tests.http_helpers import RunningApp
import web


ADMIN_PASSWORD = "administrator-secret-password1"
USER_PASSWORD = "ordinary-user-secret-password1"
LOG_FIELDS = {
    "timestamp", "request_id", "remote_ip", "user_id", "method", "route",
    "status", "duration_ms", "exception",
}


class _CapturedLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
        self.changed = threading.Condition()

    def emit(self, record):
        with self.changed:
            self.records.append(record)
            self.changed.notify_all()

    def requests(self, expected):
        with self.changed:
            self.changed.wait_for(
                lambda: len(self._web_records()) >= expected, timeout=1
            )
            return list(self._web_records())

    def _web_records(self):
        return [record for record in self.records if record.name == web._LOGGER.name]

    def text(self):
        formatter = logging.Formatter()
        with self.changed:
            return "\n".join(formatter.format(record) for record in self.records)


class RequestSecurityTest(unittest.TestCase):
    def setUp(self):
        self.running = RunningApp().__enter__()
        self.addCleanup(self.running.__exit__, None, None, None)
        self.client = self.running.client
        self.application = self.running.server.application
        self.logs = _CapturedLogs()
        root = logging.getLogger()
        root.addHandler(self.logs)
        self.addCleanup(root.removeHandler, self.logs)
        old_level = web._LOGGER.level
        web._LOGGER.setLevel(logging.INFO)
        self.addCleanup(web._LOGGER.setLevel, old_level)

    def admin_session(self):
        admin = self.running.users.setup_admin(
            "admin", ADMIN_PASSWORD, "Admin Name", "Finance"
        )
        session = self.application.session_service.issue(admin)
        return admin, session

    @staticmethod
    def session_headers(session):
        return {
            "Cookie": f"reimbursement_session={session.token}",
            "X-CSRF-Token": session.csrf_token,
            "Content-Type": "application/json",
        }

    def request_logs(self, count):
        records = self.logs.requests(count)
        self.assertEqual(len(records), count, "one JSON completion log is required per request")
        payloads = []
        for record in records:
            self.assertIsNone(record.exc_info, "request logs must not retain traceback data")
            self.assertIsNone(record.stack_info)
            try:
                payload = json.loads(record.getMessage())
            except (TypeError, ValueError) as error:
                self.fail(f"request log is not a JSON object: {type(error).__name__}")
            self.assertIsInstance(payload, dict)
            self.assertEqual(set(payload), LOG_FIELDS)
            self.assertRegex(payload["request_id"], r"^[0-9a-f]{16}$")
            timestamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
            self.assertIsNotNone(timestamp.utcoffset())
            self.assertEqual(payload["remote_ip"], "127.0.0.1")
            self.assertRegex(payload["route"], r"^[a-z][a-z_]*$")
            self.assertIn(payload["method"], {
                "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
                "CONNECT", "TRACE", "OTHER", "UNKNOWN",
            })
            self.assertIsInstance(payload["duration_ms"], (int, float))
            self.assertGreaterEqual(payload["duration_ms"], 0)
            self.assertLess(payload["duration_ms"], 30000)
            self.assertTrue(payload["user_id"] is None or type(payload["user_id"]) is int)
            self.assertTrue(payload["exception"] is None or isinstance(payload["exception"], str))
            payloads.append(payload)
        return payloads

    def assert_not_logged(self, *values):
        text = self.logs.text()
        for value in values:
            with self.subTest(secret=value):
                self.assertNotIn(value, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn(str(self.running.root), text)
        self.assertNotIn(str(app.__file__), text)
        self.assertNotIn(str(web.__file__), text)

    def raw_exchange(self, request, *, shutdown_write=True):
        with socket.create_connection(self.running.server.server_address, timeout=3) as connection:
            connection.sendall(request)
            if shutdown_write:
                connection.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                try:
                    chunk = connection.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def test_health_logs_one_stable_server_generated_id_for_each_request(self):
        with mock.patch("secrets.token_hex", wraps=secrets.token_hex) as token_hex:
            responses = [
                self.client.request("GET", "/healthz?token=query-secret-one", headers={
                    "X-Request-ID": "client-supplied-secret-id",
                    "X-Forwarded-For": "192.0.2.123",
                }),
                self.client.get("/healthz?token=query-secret-two"),
            ]
            logs = self.request_logs(2)
        self.assertEqual(token_hex.call_args_list, [mock.call(8), mock.call(8)])
        self.assertNotEqual(logs[0]["request_id"], logs[1]["request_id"])
        for response, entry in zip(responses, logs):
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["X-Request-ID"], entry["request_id"])
            self.assertEqual(entry["route"], "health")
            self.assertEqual(entry["method"], "GET")
            self.assertEqual(entry["status"], response.status)
            self.assertIsNone(entry["user_id"])
            self.assertIsNone(entry["exception"])
        self.assert_not_logged("query-secret-one", "query-secret-two", "client-supplied-secret-id", "192.0.2.123")

    def test_authenticated_request_records_user_id_without_session_or_csrf_tokens(self):
        admin, session = self.admin_session()
        response = self.client.request(
            "GET", "/api/session?private=query-secret", headers=self.session_headers(session)
        )
        self.assertEqual(response.status, 200)
        entry, = self.request_logs(1)
        self.assertEqual(entry["user_id"], admin.id)
        self.assertEqual(entry["route"], "session")
        self.assertEqual(entry["status"], 200)
        self.assertEqual(response.headers["X-Request-ID"], entry["request_id"])
        self.assert_not_logged(session.token, session.csrf_token, "query-secret", "Admin Name", "Finance")

    def test_failed_login_does_not_log_credentials_headers_cookies_or_body(self):
        self.admin_session()
        csrf = self.application.anonymous_csrf.issue("POST", "/api/login")
        response = self.client.request(
            "POST", "/api/login?private=query-secret",
            body=json.dumps({
                "username": "unknown-private-username", "password": "body-secret-password",
                "details": "form-private-data",
            }).encode(),
            headers={
                "Content-Type": "application/json", "X-CSRF-Token": csrf,
                "Cookie": "reimbursement_session=cookie-secret-token",
                "Authorization": "Bearer header-secret-token",
                "Referer": "https://example.test/private-referer",
            },
        )
        self.assertEqual(response.status, 401)
        entry, = self.request_logs(1)
        self.assertEqual(entry["route"], "login")
        self.assertEqual(entry["status"], 401)
        self.assertIsNone(entry["user_id"])
        self.assert_not_logged(
            csrf, "unknown-private-username", "body-secret-password", "form-private-data",
            "cookie-secret-token", "header-secret-token", "query-secret", "private-referer",
        )

    def test_unknown_paths_and_methods_use_fixed_log_names(self):
        responses = [
            self.client.get("/unknown-private-path?token=private-query"),
            self.client.request("PRIVATESECRET", "/healthz"),
            self.client.request("HEAD", "/healthz"),
            self.client.get("/static/private-file.js"),
        ]
        self.assertEqual([response.status for response in responses], [404, 405, 405, 404])
        self.assertEqual(responses[2].body, b"")
        logs = self.request_logs(4)
        self.assertEqual(logs[0]["route"], "unknown_route")
        self.assertEqual(logs[1]["method"], "OTHER")
        self.assertEqual(logs[2]["method"], "HEAD")
        self.assertEqual(logs[3]["route"], "static")
        for response, entry in zip(responses, logs):
            self.assertEqual(entry["status"], response.status)
            self.assertEqual(response.headers["X-Request-ID"], entry["request_id"])
        self.assert_not_logged("unknown-private-path", "private-query", "PRIVATESECRET", "private-file.js")

    def test_parser_failures_have_correlated_logs_without_raw_request_data(self):
        cases = (
            (400, "GET", b"GET /parser-path-secret extra HTTP/1.1\r\n\r\n"),
            (400, "HEAD", b"HEAD /parser-path-secret BADVERSION\r\n\r\n"),
            (414, "GET", b"GET /parser-path-secret/" + b"x" * 65536 + b" HTTP/1.1\r\n\r\n"),
            (431, "HEAD", b"HEAD /healthz HTTP/1.1\r\nX-Secret: parser-header-secret" + b"x" * 65536 + b"\r\n\r\n"),
        )
        for index, (status, method, request) in enumerate(cases, 1):
            with self.subTest(status=status, method=method):
                response = self.raw_exchange(request)
                head, body = response.split(b"\r\n\r\n", 1)
                self.assertIn(f" {status} ".encode(), head.split(b"\r\n", 1)[0])
                entry = self.request_logs(index)[-1]
                self.assertEqual(entry["route"], "parser_error")
                self.assertEqual(entry["method"], method)
                self.assertEqual(entry["status"], status)
                self.assertIn(f"X-Request-ID: {entry['request_id']}".encode(), head)
                if method == "HEAD":
                    self.assertEqual(body, b"")
        self.assert_not_logged("parser-path-secret", "parser-header-secret", "BADVERSION")

    def test_incomplete_request_timeout_is_logged_without_partial_path(self):
        self.running.server.set_request_limits(timeout_seconds=0.1, max_concurrent_requests=4)
        response = self.raw_exchange(
            b"GET /timeout-path-secret HTTP/1.", shutdown_write=False
        )
        self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])
        entry, = self.request_logs(1)
        self.assertEqual((entry["route"], entry["status"], entry["method"]), ("parser_error", 408, "GET"))
        self.assert_not_logged("timeout-path-secret")

    def test_unsupported_http_version_returns_safe_json_and_head_headers(self):
        for index, method in enumerate(("GET", "HEAD"), 1):
            with self.subTest(method=method):
                response = self.raw_exchange(
                    f"{method} /version-secret-path HTTP/9.7\r\n\r\n".encode()
                )
                self.assertTrue(response.startswith(b"HTTP/1.0 505 "))
                head, body = response.split(b"\r\n\r\n", 1)
                self.assertIn(b"Content-Type: application/json", head)
                if method == "HEAD":
                    self.assertEqual(body, b"")
                else:
                    self.assertEqual(json.loads(body), {"error": "不支持此 HTTP 版本，请使用 HTTP/1.1"})
                entry = self.request_logs(index)[-1]
                self.assertEqual((entry["route"], entry["status"], entry["method"]), ("parser_error", 505, method))
        self.assert_not_logged("version-secret-path", "HTTP/9.7")

    def test_saturated_request_has_correlated_safe_503_log(self):
        with mock.patch.object(self.running.server._request_slots, "acquire", return_value=False):
            response = self.raw_exchange(
                b"GET /capacity-secret-path HTTP/1.0\r\nCookie: capacity-secret-token\r\n\r\n"
            )
            entry, = self.request_logs(1)
        head, body = response.split(b"\r\n\r\n", 1)
        self.assertIn(b" 503 ", head.split(b"\r\n", 1)[0])
        self.assertEqual(entry["status"], 503)
        self.assertEqual(entry["route"], "rejected")
        self.assertIn(f"X-Request-ID: {entry['request_id']}".encode(), head)
        self.assertIn("error", json.loads(body))
        self.assert_not_logged("capacity-secret-path", "capacity-secret-token")

    def test_static_open_failure_after_headers_never_sends_second_response(self):
        from pathlib import Path

        original_open = Path.open
        static_file = (self.running.static_dir / "app.js").resolve()

        def open_file(path, *args, **kwargs):
            if path == static_file:
                raise RuntimeError("static-secret-message")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", open_file):
            response = self.raw_exchange(b"GET /static/app.js HTTP/1.0\r\n\r\n")
            entry, = self.request_logs(1)
        self.assertEqual(response.count(b"HTTP/1.0 "), 1)
        self.assertEqual((entry["status"], entry["exception"]), (200, "RuntimeError"))
        self.assertEqual(entry["route"], "static")
        self.assert_not_logged("static-secret-message")

    def test_callback_failure_logs_only_exception_class_and_returns_safe_500(self):
        with mock.patch.object(self.application, "_health", side_effect=ValueError(
            f"callback-secret-message at {self.running.root}/private-file.xlsx"
        )):
            response = self.client.get("/healthz?password=query-secret")
            entry, = self.request_logs(1)
        self.assertEqual(response.status, 500)
        self.assertEqual(entry["status"], 500)
        self.assertEqual(entry["exception"], "ValueError")
        self.assertEqual(entry["route"], "health")
        self.assertEqual(response.headers["X-Request-ID"], entry["request_id"])
        self.assertNotIn(b"callback-secret-message", response.body)
        self.assert_not_logged("callback-secret-message", "query-secret", "private-file.xlsx")

    def test_guard_failure_uses_the_same_safe_exception_record(self):
        with mock.patch.object(
            self.running.users, "setup_complete", side_effect=RuntimeError("guard-secret-message")
        ):
            response = self.client.get("/healthz")
            entry, = self.request_logs(1)
        self.assertEqual(response.status, 500)
        self.assertEqual((entry["status"], entry["exception"]), (500, "RuntimeError"))
        self.assert_not_logged("guard-secret-message")

    def test_failure_after_committed_response_logs_actual_status_without_second_response(self):
        def respond_then_fail(handler, _user, _token):
            handler.send_response(201)
            handler.send_header("Content-Length", "4")
            handler.end_headers()
            handler.wfile.write(b"done")
            raise ValueError("post-commit-secret-message")

        with mock.patch.object(self.application, "_health", side_effect=respond_then_fail):
            response = self.raw_exchange(b"GET /healthz HTTP/1.0\r\n\r\n")
            entry, = self.request_logs(1)
        self.assertEqual(response.count(b"HTTP/1.0 "), 1)
        self.assertTrue(response.endswith(b"\r\n\r\ndone"))
        self.assertEqual(entry["status"], 201)
        self.assertEqual(entry["exception"], "ValueError")
        self.assertIn(f"X-Request-ID: {entry['request_id']}".encode(), response)
        self.assert_not_logged("post-commit-secret-message")

    def test_disconnect_after_committed_response_is_logged_without_traceback(self):
        def respond_then_disconnect(handler, _user, _token):
            handler.send_response(204)
            handler.end_headers()
            raise BrokenPipeError("disconnect-secret-message")

        with mock.patch.object(self.application, "_health", side_effect=respond_then_disconnect):
            response = self.raw_exchange(b"GET /healthz HTTP/1.0\r\n\r\n")
            entry, = self.request_logs(1)
        self.assertEqual(response.count(b"HTTP/1.0 "), 1)
        self.assertEqual(entry["status"], 204)
        self.assertEqual(entry["exception"], "BrokenPipeError")
        self.assert_not_logged("disconnect-secret-message")

    def test_password_reset_response_secret_never_appears_in_logs(self):
        admin, session = self.admin_session()
        pending = self.running.users.register("alice", USER_PASSWORD, "Private Name", "Private Department")
        user = self.running.users.approve(admin.id, pending.id)
        response = self.client.request(
            "POST", f"/api/admin/users/{user.id}/reset-password", body=b"{}",
            headers=self.session_headers(session),
        )
        self.assertEqual(response.status, 200)
        temporary_password = response.json()["temporary_password"]
        self.assertTrue(temporary_password)
        entry, = self.request_logs(1)
        self.assertEqual(entry["user_id"], admin.id)
        self.assertEqual(entry["status"], 200)
        self.assertNotIn(str(user.id), entry["route"])
        self.assert_not_logged(temporary_password, session.token, session.csrf_token, USER_PASSWORD)

    def test_password_reset_revocation_failure_does_not_leak_domain_traceback(self):
        admin, session = self.admin_session()
        pending = self.running.users.register("alice", USER_PASSWORD, "Private Name", "Private Department")
        user = self.running.users.approve(admin.id, pending.id)
        with mock.patch.object(
            self.running.users, "_revoke_sessions",
            side_effect=RuntimeError(f"revocation-secret-message {self.running.root}/private-database.db"),
        ):
            response = self.client.request(
                "POST", f"/api/admin/users/{user.id}/reset-password", body=b"{}",
                headers=self.session_headers(session),
            )
            entry, = self.request_logs(1)
        self.assertEqual(response.status, 500)
        self.assertEqual(entry["exception"], "RuntimeError")
        self.assertEqual(entry["user_id"], admin.id)
        self.assertNotIn("temporary_password", response.json())
        self.assert_not_logged("revocation-secret-message", "private-database.db", session.token, session.csrf_token)


class RequestLoggingStartupTest(unittest.TestCase):
    def test_main_enables_info_json_logging(self):
        server = mock.Mock(server_address=("127.0.0.1", 8800))
        message = '{"request_id":"0123456789abcdef"}'
        server.serve_forever.side_effect = lambda: web._LOGGER.info(message)
        root = logging.getLogger()
        output = io.StringIO()
        with mock.patch.object(root, "handlers", []), mock.patch.object(root, "level", logging.WARNING), mock.patch.object(
            web._LOGGER, "level", logging.NOTSET
        ), mock.patch("app.create_server", return_value=server), redirect_stderr(output), redirect_stdout(io.StringIO()):
            try:
                app.main()
                self.assertEqual(output.getvalue().strip(), message)
            finally:
                for handler in root.handlers:
                    handler.close()


if __name__ == "__main__":
    unittest.main()
