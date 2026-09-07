from pathlib import Path
import json
import socket
import threading
import time
import unittest
from unittest import mock

import app
from tests.http_helpers import RunningApp


APP_DIR = Path(__file__).resolve().parents[1]


class ServerTest(unittest.TestCase):
    @staticmethod
    def _read_socket(sock: socket.socket) -> bytes:
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:
                return b"".join(chunks)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    @staticmethod
    def _wait_for_active(server, expected: int) -> None:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if server.active_request_count == expected:
                return
            time.sleep(0.005)
        raise AssertionError(f"expected {expected} active requests")

    @classmethod
    def _raw_exchange(cls, server, request: bytes, *, shutdown_write: bool = True) -> bytes:
        sock = socket.create_connection(server.server_address, timeout=1)
        try:
            sock.sendall(request)
            if shutdown_write:
                sock.shutdown(socket.SHUT_WR)
            return cls._read_socket(sock)
        finally:
            sock.close()

    def test_running_app_rolls_back_temporary_directory_when_server_creation_fails(self):
        running = RunningApp()
        with mock.patch(
            "tests.http_helpers.app.create_server", side_effect=RuntimeError("bind failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                running.__enter__()
        self.assertFalse(running.root.exists())

    def test_running_app_closes_server_and_temporary_directory_when_thread_start_fails(self):
        original_create_server = app.create_server
        created = []

        def tracked_create_server(config_path):
            server = original_create_server(config_path)
            server.shutdown = mock.Mock(wraps=server.shutdown)
            server.server_close = mock.Mock(wraps=server.server_close)
            created.append(server)
            return server

        running = RunningApp()
        try:
            with mock.patch(
                "tests.http_helpers.app.create_server", side_effect=tracked_create_server
            ), mock.patch(
                "tests.http_helpers.threading.Thread.start",
                side_effect=RuntimeError("thread failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "thread failed"):
                    running.__enter__()
            created[0].shutdown.assert_not_called()
            created[0].server_close.assert_called_once_with()
            self.assertFalse(running.root.exists())
        finally:
            if created and created[0].fileno() != -1:
                created[0].server_close()
            temporary = getattr(running, "_temporary", None)
            if temporary is not None:
                temporary.cleanup()

    def test_parser_errors_are_safe_json_without_runtime_version_or_head_body(self):
        with RunningApp() as running:
            requests = (
                ("bad-get", 400, b"GET / extra HTTP/1.1\r\nHost: localhost\r\n\r\n", False),
                ("bad-head", 400, b"HEAD / extra HTTP/1.1\r\nHost: localhost\r\n\r\n", True),
                ("version-get", 400, b"GET / NOTHTTP/1.1\r\n\r\n", False),
                ("version-head", 400, b"HEAD / NOTHTTP/1.1\r\n\r\n", True),
                ("long-get", 414, b"GET /" + b"a" * 65536 + b" HTTP/1.1\r\n\r\n", False),
                ("long-head", 414, b"HEAD /" + b"a" * 65536 + b" HTTP/1.1\r\n\r\n", True),
                (
                    "header-head",
                    431,
                    b"HEAD / HTTP/1.1\r\nHost: localhost\r\nX-Large: "
                    + b"a" * 65536
                    + b"\r\n\r\n",
                    True,
                ),
            )
            for name, expected_status, request, is_head in requests:
                with self.subTest(name=name):
                    response = self._raw_exchange(running.server, request)
                    head, body = response.split(b"\r\n\r\n", 1)
                    self.assertIn(f" {expected_status} ".encode(), head.split(b"\r\n", 1)[0])
                    self.assertIn(b"Content-Type: application/json; charset=utf-8", head)
                    self.assertIn(b"Cache-Control: no-store", head)
                    self.assertIn(b"X-Content-Type-Options: nosniff", head)
                    self.assertIn(b"Server: ReimbursementTool/2.0\r\n", head + b"\r\n")
                    self.assertNotIn(b"Python/", head)
                    if is_head:
                        self.assertEqual(body, b"")
                    else:
                        self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_partial_request_line_times_out_with_408_json(self):
        with RunningApp() as running:
            running.server.set_request_limits(timeout_seconds=0.1, max_concurrent_requests=4)
            for method, expect_body in ((b"GET", True), (b"HEAD", False)):
                with self.subTest(method=method):
                    response = self._raw_exchange(
                        running.server,
                        method + b" /healthz HTTP/1.",
                        shutdown_write=False,
                    )
                    head, body = response.split(b"\r\n\r\n", 1)
                    self.assertIn(b" 408 ", head.split(b"\r\n", 1)[0])
                    self.assertIn(b"Content-Type: application/json; charset=utf-8", head)
                    self.assertIn(b"Cache-Control: no-store", head)
                    self.assertIn(b"X-Content-Type-Options: nosniff", head)
                    if expect_body:
                        self.assertIn("error", json.loads(body.decode("utf-8")))
                    else:
                        self.assertEqual(body, b"")

    def test_request_admission_is_atomic_with_runtime_limit_replacement(self):
        entered_acquire = threading.Event()
        allow_acquire = threading.Event()
        setter_done = threading.Event()
        request_errors = []
        setter_errors = []

        class DelayedSemaphore:
            def acquire(self, *, blocking):
                self.assert_nonblocking = blocking
                entered_acquire.set()
                allow_acquire.wait(timeout=1)
                return True

            def release(self):
                return None

        server = object.__new__(app.BoundedThreadingHTTPServer)
        server.request_idle_timeout = 1
        server.max_concurrent_requests = 1
        server._request_slots = DelayedSemaphore()
        server._request_count_lock = threading.Lock()
        server._active_request_count = 0

        def admit_request():
            try:
                server.process_request(mock.Mock(), ("127.0.0.1", 1))
            except BaseException as error:
                request_errors.append(error)

        def replace_limits():
            try:
                server.set_request_limits(timeout_seconds=2, max_concurrent_requests=2)
            except BaseException as error:
                setter_errors.append(error)
            finally:
                setter_done.set()

        with mock.patch.object(
            app.ThreadingHTTPServer,
            "process_request",
            autospec=True,
            side_effect=lambda current, _request, _address: current._release_request_slot(),
        ):
            request_thread = threading.Thread(target=admit_request)
            setter_thread = threading.Thread(target=replace_limits)
            request_thread.start()
            self.assertTrue(entered_acquire.wait(timeout=1))
            setter_thread.start()
            try:
                setter_finished_during_admission = setter_done.wait(timeout=0.05)
            finally:
                allow_acquire.set()
                request_thread.join(timeout=1)
                setter_thread.join(timeout=1)

        self.assertFalse(setter_finished_during_admission)
        self.assertEqual(request_errors, [])
        self.assertTrue(
            not setter_errors or isinstance(setter_errors[0], RuntimeError), setter_errors
        )

    def test_saturation_rejection_drains_only_a_bounded_chunk(self):
        server = object.__new__(app.BoundedThreadingHTTPServer)
        server.request_idle_timeout = 15
        server.shutdown_request = mock.Mock()
        request = mock.Mock()
        request.recv.side_effect = [b"x" * (64 * 1024)] * 3 + [BlockingIOError]

        server._reject_saturated(request)

        request.recv.assert_called_once_with(64 * 1024)
        self.assertIn(b" 503 ", request.sendall.call_args.args[0].split(b"\r\n", 1)[0])
        server.shutdown_request.assert_called_once_with(request)

    def test_saturation_rejection_sends_body_for_unconfirmed_head_prefixes(self):
        for request_prefix in (b"", b"H", b"HE", b"HEA"):
            with self.subTest(request_prefix=request_prefix):
                server = object.__new__(app.BoundedThreadingHTTPServer)
                server.request_idle_timeout = 15
                server.shutdown_request = mock.Mock()
                request = mock.Mock()
                request.recv.return_value = request_prefix

                server._reject_saturated(request)

                response = request.sendall.call_args.args[0]
                head, body = response.split(b"\r\n\r\n", 1)
                content_length = int(
                    next(
                        line.split(b":", 1)[1]
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                )
                self.assertIn("error", json.loads(body.decode("utf-8")))
                self.assertEqual(len(body), content_length)

    def test_partial_headers_time_out_with_408_json(self):
        with RunningApp() as running:
            running.server.set_request_limits(timeout_seconds=0.1, max_concurrent_requests=4)
            response = self._raw_exchange(
                running.server,
                b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nX-Pending: ",
                shutdown_write=False,
            )
            head, body = response.split(b"\r\n\r\n", 1)
            self.assertIn(b" 408 ", head.split(b"\r\n", 1)[0])
            self.assertIn(b"Cache-Control: no-store", head)
            self.assertIn(b"X-Content-Type-Options: nosniff", head)
            self.assertIn("error", json.loads(body.decode("utf-8")))

    def test_short_and_idle_request_bodies_do_not_hold_handler_threads(self):
        with RunningApp() as running:
            running.server.set_request_limits(timeout_seconds=0.1, max_concurrent_requests=4)
            setup_token = running.client.csrf_for("/api/setup")
            body = json.dumps({
                "username": "admin", "password": "administrator1",
                "real_name": "A", "department": "D",
            }).encode()
            headers = (
                b"POST /api/setup HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body) + 20}\r\n".encode()
                + f"X-CSRF-Token: {setup_token}\r\nConnection: close\r\n\r\n".encode()
            )

            short = socket.create_connection(running.server.server_address, timeout=1)
            short.sendall(headers + body)
            short.shutdown(socket.SHUT_WR)
            short_response = self._read_socket(short)
            short.close()
            self.assertIn(b" 400 ", short_response.split(b"\r\n", 1)[0])
            self.assertIn("请求体不完整".encode(), short_response)

            idle = socket.create_connection(running.server.server_address, timeout=1)
            idle.sendall(headers + body[:1])
            idle_response = self._read_socket(idle)
            idle.close()
            self.assertIn(b" 408 ", idle_response.split(b"\r\n", 1)[0])
            self.assertIn(b"Connection: close", idle_response)

    def test_saturated_handler_limit_returns_503_without_spawning_another_thread(self):
        with RunningApp() as running:
            running.server.set_request_limits(timeout_seconds=1, max_concurrent_requests=1)
            blocker = socket.create_connection(running.server.server_address, timeout=1)
            blocker.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n")
            self._wait_for_active(running.server, 1)

            rejected = socket.create_connection(running.server.server_address, timeout=1)
            rejected.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            response = self._read_socket(rejected)
            rejected.close()
            blocker.close()
            self.assertIn(b" 503 ", response.split(b"\r\n", 1)[0])
            self.assertIn(b"Cache-Control: no-store", response)
            self.assertIn(b"X-Content-Type-Options: nosniff", response)

    def test_saturated_delayed_get_receives_complete_json_body(self):
        first_peek = threading.Event()
        request_sent = threading.Event()
        original_reject = app.BoundedThreadingHTTPServer._reject_saturated

        class DelayedReadableSocket:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.first_recv = True

            def recv(self, size):
                if self.first_recv:
                    self.first_recv = False
                    first_peek.set()
                    if not request_sent.wait(timeout=1):
                        raise AssertionError("client did not send request after accept")
                    raise BlockingIOError
                return self.wrapped.recv(size)

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        def reject_after_empty_peek(server, request):
            original_reject(server, DelayedReadableSocket(request))

        with RunningApp() as running, mock.patch.object(
            app.BoundedThreadingHTTPServer,
            "_reject_saturated",
            autospec=True,
            side_effect=reject_after_empty_peek,
        ):
            running.server.set_request_limits(timeout_seconds=1, max_concurrent_requests=1)
            blocker = socket.create_connection(running.server.server_address, timeout=1)
            rejected = None
            try:
                blocker.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n")
                self._wait_for_active(running.server, 1)
                rejected = socket.create_connection(running.server.server_address, timeout=1)
                self.assertTrue(first_peek.wait(timeout=1))
                rejected.sendall(
                    b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                )
                request_sent.set()
                rejected.shutdown(socket.SHUT_WR)
                response = self._read_socket(rejected)
            finally:
                request_sent.set()
                if rejected is not None:
                    rejected.close()
                blocker.close()

        head, body = response.split(b"\r\n\r\n", 1)
        content_length = int(
            next(
                line.split(b":", 1)[1]
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
        )
        self.assertIn(b" 503 ", head.split(b"\r\n", 1)[0])
        self.assertIn("error", json.loads(body.decode("utf-8")))
        self.assertEqual(len(body), content_length)

    def test_saturated_head_response_has_content_length_but_no_body(self):
        server = object.__new__(app.BoundedThreadingHTTPServer)
        server.request_idle_timeout = 1
        server_side, client_side = socket.socketpair()
        try:
            client_side.sendall(
                b"HEAD /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            server._reject_saturated(server_side)
            response = self._read_socket(client_side)
        finally:
            server_side.close()
            client_side.close()

        head, body = response.split(b"\r\n\r\n", 1)
        content_length = int(
            next(
                line.split(b":", 1)[1]
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
        )
        expected_body = '{"error":"服务器忙，请稍后重试"}'.encode("utf-8")
        self.assertIn(b" 503 ", head.split(b"\r\n", 1)[0])
        self.assertEqual(content_length, len(expected_body))
        self.assertEqual(body, b"")

    def test_database_and_secret_are_created_under_configured_data_directory(self):
        with RunningApp() as running:
            self.assertEqual(running.server.database.path, running.data_dir / "database" / "app.db")
            self.assertTrue((running.data_dir / "database" / "app.db").is_file())
            self.assertTrue((running.data_dir / "app-secret").is_file())

    def test_create_server_honors_explicit_lan_bind_host(self):
        with RunningApp() as running:
            running.stop()
            config = running.config_path.read_text(encoding="utf-8").replace(
                '"host": "127.0.0.1"', '"host": "0.0.0.0"'
            )
            running.config_path.write_text(config, encoding="utf-8")
            import app
            server = app.create_server(running.config_path)
            try:
                self.assertEqual(server.server_address[0], "0.0.0.0")
            finally:
                server.server_close()

    def test_protected_root_requires_setup_then_login(self):
        with RunningApp() as running:
            before_setup = running.client.get("/", follow_redirects=False)
            self.assertEqual((before_setup.status, before_setup.headers["Location"]), (303, "/setup"))
            running.client.post_json(
                "/api/setup",
                {"username": "admin", "password": "administrator1", "real_name": "A", "department": "D"},
            )
            after_setup = running.client.get("/", follow_redirects=False)
            self.assertEqual((after_setup.status, after_setup.headers["Location"]), (303, "/login"))


class FrontendContractTest(unittest.TestCase):
    """Keep the browser form aligned with the server's multipart contract."""

    @classmethod
    def setUpClass(cls):
        cls.project = APP_DIR
        cls.html = (cls.project / "templates" / "index.html").read_text(encoding="utf-8")
        cls.js = (cls.project / "static" / "app.js").read_text(encoding="utf-8")
        cls.css = (cls.project / "static" / "styles.css").read_text(encoding="utf-8")

    def test_html_has_required_controls_and_exactly_eleven_detail_rows(self):
        for field in ("date", "department", "traveler", "reason", "days", "allowance", "screenshots", "submit", "result", "error", "detail-rows"):
            self.assertRegex(self.html, rf"(?:id|name)=\"{field}\"")
        self.assertEqual(self.html.count('class="detail-row"'), 11)
        for field in ("date", "origin", "destination", "transport", "public_amount", "mileage", "toll", "lodging", "receipts"):
            self.assertIn(f' data-field="{field}"', self.html)
        self.assertIn("生成 Excel + PDF", self.html)
        self.assertIn('accept=".jpg,.jpeg,.png,image/jpeg,image/png"', self.html)
        self.assertRegex(self.html, r'<label[^>]+for="screenshots"[^>]*>')
        self.assertIn('id="date" name="date" type="date"', self.html)
        self.assertNotRegex(self.html, r'<input id="date"[^>]*\brequired\b')
        for row_number in range(1, 12):
            self.assertRegex(
                self.html,
                rf'<input data-field="date" name="row-{row_number}-date" type="date"[^>]*>',
            )
        self.assertNotRegex(self.html, r'<input id="date"[^>]*\bvalue="')
        self.assertNotRegex(self.html, r'<input data-field="date"[^>]*\bvalue="')

    def test_javascript_collects_rows_validates_and_posts_multipart(self):
        for token in ("FormData", "append('payload'", "append('screenshots'", "detail-rows", "11", "days", "allowance", "非负", "必填", "disabled", "xlsx_url", "pdf_url", "revokeObjectURL", "createElement('a'", "normalizeDateInput", "target", "_blank", "noopener"):
            self.assertIn(token, self.js)
        self.assertRegex(self.js, r"querySelectorAll\([^)]*detail-row")
        self.assertRegex(self.js, r"entry\[0\]\s*===\s*['\"]pdf_url['\"]")
        self.assertNotIn("new Date", self.js)
        self.assertNotIn("result.innerHTML", self.js)
        self.assertNotIn("validDate", self.js)

    def test_css_covers_printing_focus_and_numeric_alignment(self):
        for token in ("@media print", ":focus", "input[type=\"number\"]", "text-align: right"):
            self.assertIn(token, self.css)


if __name__ == "__main__":
    unittest.main()
