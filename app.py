"""Reimbursement HTTP server composition and command-line entry point."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import socket
import threading
import time

from config import load_config
from database import Database
from security import AnonymousCsrfSigner, PasswordHasher, load_or_create_secret
from sessions import SessionService
from users import UserService
from web import WebApplication


DEFAULT_REQUEST_IDLE_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_CONCURRENT_REQUESTS = 32


class _DeadlineSocketReader(io.RawIOBase):
    """Apply one monotonic deadline across every raw read in a request phase."""

    def __init__(self, connection: socket.socket, timeout_seconds: float):
        super().__init__()
        self._connection = connection
        self._default_timeout = timeout_seconds
        self._deadline_sequence = 0
        self._active_deadline: tuple[int, float] | None = None

    def readable(self) -> bool:
        return True

    def begin_deadline(self) -> int:
        self._deadline_sequence += 1
        token = self._deadline_sequence
        self._active_deadline = (
            token,
            time.monotonic() + self._default_timeout,
        )
        return token

    def finish_deadline(self, token: int) -> None:
        if self._active_deadline is None or self._active_deadline[0] != token:
            return
        self._active_deadline = None
        try:
            self._connection.settimeout(self._default_timeout)
        except OSError:
            pass

    def readinto(self, buffer) -> int:
        if not buffer:
            return 0
        timeout = self._default_timeout
        if self._active_deadline is not None:
            remaining = self._active_deadline[1] - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request phase deadline expired")
            timeout = min(timeout, remaining)
        self._connection.settimeout(timeout)
        try:
            return self._connection.recv_into(buffer)
        except TimeoutError as error:
            raise TimeoutError("request phase deadline expired") from error


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server with non-blocking admission control."""

    daemon_threads = True

    def __init__(self, server_address, request_handler_class):
        self.request_idle_timeout = DEFAULT_REQUEST_IDLE_TIMEOUT_SECONDS
        self.max_concurrent_requests = DEFAULT_MAX_CONCURRENT_REQUESTS
        self._request_slots = threading.BoundedSemaphore(self.max_concurrent_requests)
        self._request_count_lock = threading.Lock()
        self._active_request_count = 0
        super().__init__(server_address, request_handler_class)

    @property
    def active_request_count(self) -> int:
        with self._request_count_lock:
            return self._active_request_count

    def set_request_limits(
        self, *, timeout_seconds: float, max_concurrent_requests: int
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or type(max_concurrent_requests) is not int
            or max_concurrent_requests <= 0
        ):
            raise ValueError("request limits must be positive")
        with self._request_count_lock:
            if self._active_request_count:
                raise RuntimeError("request limits cannot change while requests are active")
            self.request_idle_timeout = float(timeout_seconds)
            self.max_concurrent_requests = max_concurrent_requests
            self._request_slots = threading.BoundedSemaphore(max_concurrent_requests)

    def process_request(self, request, client_address) -> None:
        with self._request_count_lock:
            admitted = self._request_slots.acquire(blocking=False)
            if admitted:
                self._active_request_count += 1
        if not admitted:
            self._reject_saturated(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_request_slot()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_request_slot()

    def _release_request_slot(self) -> None:
        with self._request_count_lock:
            self._active_request_count -= 1
            self._request_slots.release()

    def _reject_saturated(self, request) -> None:
        body = '{"error":"\u670d\u52a1\u5668\u5fd9\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5"}'.encode("utf-8")
        response_head = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Cache-Control: no-store\r\n"
            b"X-Content-Type-Options: nosniff\r\n"
            b"Connection: close\r\n\r\n"
        )
        request_prefix = b""
        try:
            request.setblocking(False)
            try:
                request_prefix = request.recv(64 * 1024)
            except BlockingIOError:
                pass
            request.settimeout(min(self.request_idle_timeout, 1.0))
            confirmed_head = request_prefix.startswith(b"HEAD ")
            request.sendall(response_head if confirmed_head else response_head + body)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)


def create_server(config_path: Path) -> ThreadingHTTPServer:
    """Create the configured server without starting its serving loop."""
    config = load_config(Path(config_path), allow_ephemeral_port=True)
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = Database(config.data_dir / "database" / "app.db")
    database.migrate()
    secret = load_or_create_secret(config.data_dir / "app-secret")

    password_hasher = PasswordHasher()
    session_service = SessionService(database)
    user_service = UserService(
        database,
        password_hasher,
        revoke_sessions=session_service.revoke_user,
    )
    application = WebApplication(
        config,
        user_service,
        session_service,
        AnonymousCsrfSigner(secret),
    )

    class Handler(BaseHTTPRequestHandler):
        server_version = "ReimbursementTool/2.0"

        def version_string(self) -> str:
            return self.server_version

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(self.server.request_idle_timeout)
            self.rfile.close()
            self._deadline_reader = _DeadlineSocketReader(
                self.connection, self.server.request_idle_timeout
            )
            self.rfile = io.BufferedReader(self._deadline_reader)

        def handle_one_request(self) -> None:
            deadline_token = self._deadline_reader.begin_deadline()
            read_timed_out = False
            read_failed = False
            parsed = False
            try:
                try:
                    self.raw_requestline = self._read_request_line(65537)
                except TimeoutError:
                    read_timed_out = True
                except OSError:
                    read_failed = True
                if not read_timed_out and not read_failed and self.raw_requestline:
                    if len(self.raw_requestline) <= 65536:
                        try:
                            parsed = self.parse_request()
                        except TimeoutError:
                            read_timed_out = True
                        except OSError:
                            read_failed = True
            finally:
                self._deadline_reader.finish_deadline(deadline_token)

            if read_timed_out:
                self._request_timed_out()
                return
            if read_failed:
                self.close_connection = True
                return
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not parsed:
                return
            method_name = "do_" + self.command
            if not hasattr(self, method_name):
                self.send_error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    f"Unsupported method ({self.command!r})",
                )
                return
            getattr(self, method_name)()
            try:
                self.wfile.flush()
            except TimeoutError:
                self.close_connection = True

        def read_request_body(self, length: int) -> bytes:
            deadline_token = self._deadline_reader.begin_deadline()
            try:
                return self.rfile.read(length)
            finally:
                self._deadline_reader.finish_deadline(deadline_token)

        def _read_request_line(self, limit: int) -> bytes:
            consumed = bytearray()
            self.raw_requestline = b""
            while len(consumed) < limit:
                available = self.rfile.peek(limit - len(consumed))
                if not available:
                    break
                available = available[:limit - len(consumed)]
                newline = available.find(b"\n")
                take = newline + 1 if newline >= 0 else len(available)
                consumed.extend(self.rfile.read(take))
                self.raw_requestline = bytes(consumed)
                if consumed.endswith(b"\n"):
                    break
            return bytes(consumed)

        def _request_timed_out(self) -> None:
            self.close_connection = True
            if not hasattr(self, "raw_requestline"):
                self.raw_requestline = b""
            if not hasattr(self, "requestline"):
                self.requestline = ""
            if not hasattr(self, "command"):
                self.command = ""
            self.send_error(HTTPStatus.REQUEST_TIMEOUT)

        def do_GET(self) -> None:  # noqa: N802
            application.handle_get(self)

        def do_POST(self) -> None:  # noqa: N802
            application.handle_post(self)

        def do_PUT(self) -> None:  # noqa: N802
            application.handle_unsupported(self)

        def do_PATCH(self) -> None:  # noqa: N802
            application.handle_unsupported(self)

        def do_DELETE(self) -> None:  # noqa: N802
            application.handle_unsupported(self)

        def do_HEAD(self) -> None:  # noqa: N802
            application.handle_unsupported(self)

        def do_OPTIONS(self) -> None:  # noqa: N802
            application.handle_unsupported(self)

        def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
            if code == 501:
                application.handle_unsupported(self)
                return
            if code in {400, 408, 414, 431}:
                application.handle_parser_error(self, code)
                return
            super().send_error(code, message, explain)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = BoundedThreadingHTTPServer((config.host, config.port), Handler)
    server.config = config
    server.max_body_bytes = config.max_body_bytes
    server.application = application
    server.database = database
    return server


def main() -> None:
    config_path = Path(__file__).resolve().with_name("config.json")
    server = create_server(config_path)
    host, port = server.server_address[:2]
    print(
        f"http://127.0.0.1:{port}" if host in {"0.0.0.0", "::"} else f"http://{host}:{port}",
        flush=True,
    )
    if host in {"0.0.0.0", "::"}:
        try:
            addresses = socket.gethostbyname_ex(socket.gethostname())[2]
        except OSError:
            addresses = []
        for address in sorted(set(addresses)):
            if not address.startswith("127."):
                print(f"LAN: http://{address}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
