"""Reimbursement HTTP server composition and command-line entry point."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket

from config import load_config
from database import Database
from security import AnonymousCsrfSigner, PasswordHasher, load_or_create_secret
from sessions import SessionService
from users import UserService
from web import WebApplication


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
            super().send_error(code, message, explain)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((config.host, config.port), Handler)
    server.daemon_threads = True
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
