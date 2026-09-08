"""Guarded HTTP routes for setup, authentication, and account sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import hashlib
from http import cookies
from http.server import BaseHTTPRequestHandler
import html
import json
import logging
import math
import mimetypes
from pathlib import Path, PurePosixPath
import tempfile
import threading
import time
from typing import Callable, Hashable, Iterable
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

from config import AppConfig
from reimbursements import (
    ReimbursementGenerationError,
    ReimbursementNotFound,
    ReimbursementRecord,
    ReimbursementService,
    ReimbursementStorageError,
)
from security import AnonymousCsrfSigner
from sessions import AuthenticatedUser, SessionService
from users import (
    AuthenticationFailed,
    SetupClosed,
    UserError,
    UserService,
    UsernameTaken,
    ValidationError,
    canonicalize_username,
)
from validation import (
    ValidationError as ReimbursementValidationError,
    validate_image_filename,
    validate_payload,
)


_LOGGER = logging.getLogger(__name__)
_COOKIE_NAME = "reimbursement_session"
_COOKIE_MAX_AGE = 7 * 24 * 60 * 60
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 15 * 60
RATE_LIMIT_MAX_KEYS = 4096
LOGIN_IP_RATE_LIMIT_ATTEMPTS = 10
LOGIN_GLOBAL_RATE_LIMIT_ATTEMPTS = 100
_INVALID_LOGIN_USERNAME_KEY = ("invalid", "<invalid>")
_REIMBURSEMENT_PREFIX = "/api/reimbursements/"
_XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
_STATIC_EXTENSIONS = {
    ".css", ".js", ".html", ".ico", ".png", ".jpg", ".jpeg", ".svg", ".webp",
    ".woff", ".woff2", ".ttf",
}


@dataclass(frozen=True)
class Route:
    callback: str
    setup: str = "complete"
    authentication: bool = False
    roles: tuple[str, ...] = ()
    allow_forced_password_change: bool = False
    csrf: str | None = None


class AttemptRateLimiter:
    """Reserve bounded attempts in fixed windows without background threads."""

    def __init__(
        self,
        limit: int = RATE_LIMIT_ATTEMPTS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = RATE_LIMIT_MAX_KEYS,
    ):
        if (
            type(limit) is not int
            or limit <= 0
            or window_seconds <= 0
            or type(max_keys) is not int
            or max_keys <= 0
        ):
            raise ValueError("rate limit values must be positive")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._attempts: dict[Hashable, list[float]] = {}

    def reserve(self, key: Hashable) -> int | None:
        return self.reserve_many(((key, self.limit),))

    def reserve_many(
        self, reservations: Iterable[tuple[Hashable, int]]
    ) -> int | None:
        limits: dict[Hashable, int] = {}
        for key, limit in reservations:
            if type(limit) is not int or limit <= 0:
                raise ValueError("rate limit values must be positive")
            prior_limit = limits.setdefault(key, limit)
            if prior_limit != limit:
                raise ValueError("one key cannot have multiple limits")
        if not limits:
            raise ValueError("at least one rate limit reservation is required")

        with self._lock:
            now = self._clock()
            cutoff = now - self.window_seconds
            for existing_key, timestamps in list(self._attempts.items()):
                retained = [timestamp for timestamp in timestamps if timestamp > cutoff]
                if retained:
                    self._attempts[existing_key] = retained
                else:
                    del self._attempts[existing_key]

            retry_after = [
                max(1, math.ceil(timestamps[0] + self.window_seconds - now))
                for key, limit in limits.items()
                if len(timestamps := self._attempts.get(key, [])) >= limit
            ]
            if retry_after:
                return max(retry_after)

            missing_keys = sum(key not in self._attempts for key in limits)
            if len(self._attempts) + missing_keys > self.max_keys:
                if not self._attempts:
                    return max(1, math.ceil(self.window_seconds))
                return max(
                    1,
                    min(
                        math.ceil(timestamps[0] + self.window_seconds - now)
                        for timestamps in self._attempts.values()
                    ),
                )

            for key in limits:
                self._attempts.setdefault(key, []).append(now)
            return None

    def clear(self, key: Hashable) -> None:
        with self._lock:
            self._attempts.pop(key, None)


class WebApplication:
    """Apply ordered access guards and dispatch the fixed authentication routes."""

    def __init__(
        self,
        config: AppConfig,
        user_service: UserService,
        session_service: SessionService,
        anonymous_csrf: AnonymousCsrfSigner,
        reimbursement_service: ReimbursementService,
        rate_limiter: AttemptRateLimiter | None = None,
    ):
        self.config = config
        self.user_service = user_service
        self.session_service = session_service
        self.anonymous_csrf = anonymous_csrf
        self.reimbursement_service = reimbursement_service
        self.rate_limiter = rate_limiter or AttemptRateLimiter()
        self._upload_slots = threading.BoundedSemaphore(
            config.max_concurrent_generations
        )
        self.get_routes = {
            "/healthz": Route("_health", setup="any"),
            "/setup": Route("_setup_page", setup="incomplete"),
            "/login": Route("_login_page"),
            "/register": Route("_register_page"),
            "/api/session": Route(
                "_session", authentication=True, allow_forced_password_change=True
            ),
            "/api/reimbursements": Route(
                "_reimbursement_list", authentication=True, roles=("user",)
            ),
            "/change-password": Route(
                "_change_password_page", authentication=True, roles=("user",),
                allow_forced_password_change=True,
            ),
            "/": Route("_root", authentication=True, roles=("user",)),
            "/admin": Route("_admin_page", authentication=True, roles=("admin",)),
            # Task 7 replaces this placeholder; keeping the namespace guarded prevents
            # forced-change sessions from probing an upcoming administrator endpoint.
            "/api/admin/users": Route("_not_found", authentication=True, roles=("admin",)),
        }
        self.post_routes = {
            "/api/setup": Route("_setup", setup="incomplete", csrf="anonymous"),
            "/api/login": Route("_login", csrf="anonymous"),
            "/api/register": Route("_register", csrf="anonymous"),
            "/api/password/change": Route(
                "_change_password", authentication=True, roles=("user",),
                allow_forced_password_change=True, csrf="session",
            ),
            "/api/logout": Route(
                "_logout", authentication=True, allow_forced_password_change=True,
                csrf="session",
            ),
            "/api/reimbursements/generate": Route(
                "_reimbursement_generate", authentication=True, roles=("user",),
                csrf="session",
            ),
        }

    def handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        self._safely(handler, lambda: self._dispatch(handler, "GET"))

    def handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        self._safely(handler, lambda: self._dispatch(handler, "POST"))

    def handle_unsupported(self, handler: BaseHTTPRequestHandler) -> None:
        self._safely(handler, lambda: self._unsupported(handler))

    def handle_parser_error(self, handler: BaseHTTPRequestHandler, status: int) -> None:
        messages = {
            400: "请求格式错误",
            408: "请求读取超时",
            414: "请求路径过长",
            431: "请求头过大",
        }
        handler.close_connection = True
        if getattr(handler, "request_version", "HTTP/0.9") in {"", "HTTP/0.9"}:
            handler.request_version = "HTTP/1.0"
        raw_method = getattr(handler, "raw_requestline", b"").split(maxsplit=1)[:1]
        if raw_method == [b"HEAD"]:
            handler.command = "HEAD"
        try:
            self._json(
                handler,
                status,
                {"error": messages.get(status, "请求格式错误")},
                headers={"Connection": "close"},
            )
        except OSError:
            return

    def _unsupported(self, handler: BaseHTTPRequestHandler) -> None:
        path = self._decoded_path(handler.path)
        if path is None:
            self._json(handler, 404, {"error": "请求路径不存在"})
            return
        allowed = []
        if self._route_for("GET", path) is not None or path.startswith("/static/"):
            allowed.append("GET")
        if self._route_for("POST", path) is not None:
            allowed.append("POST")
        if not allowed:
            self._json(handler, 404, {"error": "请求路径不存在"})
            return
        self._method_not_allowed(handler, ", ".join(allowed))

    def _safely(self, handler: BaseHTTPRequestHandler, action: Callable[[], None]) -> None:
        try:
            action()
        except (ConnectionError, TimeoutError):
            return
        except Exception:
            _LOGGER.exception("unhandled web request failure for %s", handler.command)
            self._json(handler, 500, {"error": "服务暂时不可用，请稍后重试"})

    def _dispatch(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        path = self._decoded_path(handler.path)
        if path is None:
            self._json(handler, 404, {"error": "请求路径不存在"})
            return
        if method == "GET" and path.startswith("/static/"):
            self._static(handler, path)
            return
        if method != "GET" and path.startswith("/static/"):
            self._method_not_allowed(handler, "GET")
            return

        route = self._route_for(method, path)
        if route is None:
            other_method = "POST" if method == "GET" else "GET"
            if self._route_for(other_method, path) is not None:
                self._method_not_allowed(handler, "POST" if method == "GET" else "GET")
            else:
                self._json(handler, 404, {"error": "请求路径不存在"})
            return

        setup_complete = self.user_service.setup_complete()
        if not self._guard_setup(handler, path, route, setup_complete):
            return

        token = self._session_token(handler)
        current_user = self.session_service.resolve(token) if token else None
        if route.authentication and current_user is None:
            self._unauthenticated(handler, path, token is not None)
            return

        # Session resolution is the account-status guard: inactive accounts are
        # revoked by SessionService before any later authorization decision.
        if current_user is not None and current_user.must_change_password:
            if not route.allow_forced_password_change:
                self._forced_password_change(handler, path)
                return

        if current_user is not None and route.roles and current_user.role not in route.roles:
            self._wrong_role(handler, path, current_user)
            return

        if route.csrf == "anonymous":
            submitted = handler.headers.get("X-CSRF-Token")
            if not self.anonymous_csrf.verify(submitted, method, path):
                self._json(handler, 403, {"error": "安全校验失败，请刷新页面后重试"})
                return
        elif route.csrf == "session":
            submitted = handler.headers.get("X-CSRF-Token")
            if not self.session_service.verify_csrf(current_user, submitted):
                self._json(handler, 403, {"error": "安全校验失败，请刷新页面后重试"})
                return

        callback: Callable[[BaseHTTPRequestHandler, AuthenticatedUser | None, str | None], None]
        callback = getattr(self, route.callback)
        try:
            callback(handler, current_user, token)
        except (ConnectionError, TimeoutError):
            handler.close_connection = True
            return
        except Exception:
            _LOGGER.exception("unhandled web request failure for %s %s", method, path)
            self._json(handler, 500, {"error": "服务暂时不可用，请稍后重试"})

    def _route_for(self, method: str, path: str) -> Route | None:
        routes = self.get_routes if method == "GET" else self.post_routes
        route = routes.get(path)
        if route is not None or not path.startswith(_REIMBURSEMENT_PREFIX):
            return route
        parts = path[len(_REIMBURSEMENT_PREFIX):].split("/")
        if len(parts) != 2 or not all(parts):
            return None
        try:
            record_uuid = UUID(parts[0])
        except ValueError:
            return None
        if record_uuid.version != 4 or str(record_uuid) != parts[0]:
            return None
        if method == "GET" and parts[1] in {"xlsx", "pdf"}:
            return Route("_reimbursement_download", authentication=True)
        if method == "POST" and parts[1] in {"trash", "restore", "purge"}:
            return Route(
                "_reimbursement_lifecycle", authentication=True, roles=("user",),
                csrf="session",
            )
        return None

    def _guard_setup(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        route: Route,
        setup_complete: bool,
    ) -> bool:
        if route.setup == "any":
            return True
        if route.setup == "incomplete" and setup_complete:
            if path == "/setup":
                self._redirect(handler, "/login")
            else:
                self._json(handler, 403, {"error": "系统初始化已完成"})
            return False
        if route.setup == "complete" and not setup_complete:
            if self._is_api(path):
                self._json(handler, 403, {"error": "请先完成系统初始化"})
            else:
                self._redirect(handler, "/setup")
            return False
        return True

    def _health(self, handler, _user, _token) -> None:
        self._json(handler, 200, {"status": "ok"})

    def _setup_page(self, handler, _user, _token) -> None:
        self._auth_page(handler, "初始化系统", "/api/setup")

    def _login_page(self, handler, user, _token) -> None:
        if user is not None:
            self._redirect(handler, self._home_for(user))
            return
        self._auth_page(handler, "登录", "/api/login")

    def _register_page(self, handler, user, _token) -> None:
        if user is not None:
            self._redirect(handler, self._home_for(user))
            return
        self._auth_page(handler, "注册", "/api/register")

    def _change_password_page(self, handler, _user, _token) -> None:
        self._html(handler, 200, self._page_markup("修改密码"))

    def _root(self, handler, _user, _token) -> None:
        index_path = self.config.templates_dir / "index.html"
        if not index_path.is_file():
            self._json(handler, 500, {"error": "页面模板不可用"})
            return
        self._file(handler, index_path, "text/html; charset=utf-8", no_store=True)

    def _admin_page(self, handler, _user, _token) -> None:
        self._html(handler, 200, self._page_markup("系统管理"))

    def _setup(self, handler, _user, _token) -> None:
        payload = self._json_body(handler)
        if payload is None:
            return
        values = self._required_strings(payload, "username", "password", "real_name", "department")
        if values is None:
            self._json(handler, 400, {"error": "请完整填写初始化信息"})
            return
        if self._rate_limited(handler, ("setup", self._client_ip(handler))):
            return
        try:
            self.user_service.setup_admin(*values)
        except SetupClosed:
            self._json(handler, 403, {"error": "系统初始化已完成"})
            return
        except (UserError, ValueError):
            self._json(handler, 400, {"error": "初始化信息不符合要求"})
            return
        self._json(handler, 201, {"message": "系统初始化完成", "next": "/login"})

    def _login(self, handler, _user, _token) -> None:
        payload = self._json_body(handler)
        if payload is None:
            return
        username = payload.get("username")
        password = payload.get("password")
        username_key = self._login_username_key(username)
        client_ip = self._client_ip(handler)
        account_limit_key = (
            "login-account-ip",
            username_key,
            client_ip,
        )
        if self._rate_limited_many(
            handler,
            (
                (account_limit_key, RATE_LIMIT_ATTEMPTS),
                (("login-ip", client_ip), LOGIN_IP_RATE_LIMIT_ATTEMPTS),
                (("login-global",), LOGIN_GLOBAL_RATE_LIMIT_ATTEMPTS),
            ),
        ):
            return
        try:
            authenticated_user = self.user_service.authenticate(username, password)
            issued = self.session_service.issue(authenticated_user)
        except (AuthenticationFailed, ValueError):
            self._login_failed(handler)
            return
        self.rate_limiter.clear(account_limit_key)
        self._json(
            handler,
            200,
            {"message": "登录成功", "next": self._home_for_user_snapshot(authenticated_user)},
            cookie=self._session_cookie(issued.token),
        )

    def _register(self, handler, _user, _token) -> None:
        payload = self._json_body(handler)
        if payload is None:
            return
        values = self._required_strings(payload, "username", "password", "real_name", "department")
        if values is None:
            self._json(handler, 400, {"error": "请完整填写注册信息"})
            return
        if self._rate_limited(handler, ("register", self._client_ip(handler))):
            return
        try:
            self.user_service.register(*values)
        except UsernameTaken:
            self._json(handler, 409, {"error": "用户名已存在"})
            return
        except (UserError, ValueError):
            self._json(handler, 400, {"error": "注册信息不符合要求"})
            return
        self._json(handler, 201, {"message": "注册申请已提交，请等待管理员审批"})

    def _session(self, handler, user, _token) -> None:
        assert user is not None
        self._json(
            handler,
            200,
            {
                "user": {
                    "id": user.user_id,
                    "username": user.username,
                    "real_name": user.real_name,
                    "department": user.department,
                    "role": user.role,
                    "must_change_password": user.must_change_password,
                },
                "csrf_token": user.csrf_token,
            },
        )

    def _change_password(self, handler, user, _token) -> None:
        assert user is not None
        payload = self._json_body(handler)
        if payload is None:
            return
        values = self._required_strings(payload, "current_password", "new_password")
        if values is None:
            self._json(handler, 400, {"error": "请填写当前密码和新密码"})
            return
        try:
            self.user_service.change_password(user.user_id, *values)
        except (AuthenticationFailed, UserError, ValueError):
            self._json(handler, 400, {"error": "当前密码错误或新密码不符合要求"})
            return
        self._json(
            handler, 200, {"message": "密码已修改，请重新登录", "next": "/login"},
            cookie=self._expired_cookie(),
        )

    def _logout(self, handler, _user, token) -> None:
        if self._json_body(handler) is None:
            return
        self.session_service.revoke_token(token)
        self._json(
            handler, 200, {"message": "已退出登录", "next": "/login"},
            cookie=self._expired_cookie(),
        )

    def _reimbursement_list(self, handler, user, _token) -> None:
        assert user is not None
        parameters = parse_qs(
            urlsplit(handler.path).query,
            keep_blank_values=True,
            strict_parsing=False,
        )
        if set(parameters) != {"scope"} or len(parameters["scope"]) != 1:
            self._json(handler, 400, {"error": "报销记录范围无效"})
            return
        scope = parameters["scope"][0]
        if scope == "active":
            records = self.reimbursement_service.list_active(user.user_id)
        elif scope == "trash":
            records = self.reimbursement_service.list_trash(user.user_id)
        else:
            self._json(handler, 400, {"error": "报销记录范围无效"})
            return
        self._json(
            handler,
            200,
            {"reimbursements": [self._record_payload(record) for record in records]},
        )

    def _reimbursement_download(self, handler, user, _token) -> None:
        assert user is not None
        parts = self._reimbursement_parts(handler)
        if parts is None or parts[1] not in {"xlsx", "pdf"} or user.role != "user":
            self._record_not_found(handler)
            return
        record_id, kind = parts
        try:
            owned_file = self.reimbursement_service.owned_file(
                user.user_id, record_id, kind
            )
        except ReimbursementNotFound:
            self._record_not_found(handler)
            return
        response_started = False
        try:
            with owned_file as owned:
                content_types = {
                    "xlsx": _XLSX_CONTENT_TYPE,
                    "pdf": "application/pdf",
                }
                dispositions = {"xlsx": "attachment", "pdf": "inline"}
                response_started = True
                handler.send_response(200)
                handler.send_header("Content-Type", content_types[owned.kind])
                handler.send_header("Content-Length", str(owned.size))
                handler.send_header("X-Content-Type-Options", "nosniff")
                handler.send_header("Cache-Control", "no-store")
                handler.send_header(
                    "Content-Disposition",
                    f"{dispositions[owned.kind]}; filename*=UTF-8''"
                    + quote(owned.display_name, safe=""),
                )
                handler.end_headers()
                while True:
                    chunk = owned.stream.read(65536)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
        except OSError:
            if response_started:
                handler.close_connection = True
                return
            raise

    def _reimbursement_lifecycle(self, handler, user, _token) -> None:
        assert user is not None
        parts = self._reimbursement_parts(handler)
        if parts is None or parts[1] not in {"trash", "restore", "purge"}:
            self._json(handler, 404, {"error": "请求路径不存在"})
            return
        if user.role != "user":
            self._record_not_found(handler)
            return
        record_id, action = parts
        payload = self._json_body(handler)
        if payload is None:
            return
        confirmed = (
            set(payload) == {"confirm"}
            and type(payload.get("confirm")) is bool
            and payload["confirm"] is True
        )
        if action == "purge" and not confirmed:
            self._json(handler, 400, {"error": "请确认永久删除报销记录"})
            return
        try:
            if action == "trash":
                record = self.reimbursement_service.trash(user.user_id, record_id)
                self._json(handler, 200, {"record": self._record_payload(record)})
            elif action == "restore":
                record = self.reimbursement_service.restore(user.user_id, record_id)
                self._json(handler, 200, {"record": self._record_payload(record)})
            else:
                self.reimbursement_service.purge_one(user.user_id, record_id)
                self._json(handler, 200, {"message": "报销记录已永久删除"})
        except ReimbursementNotFound:
            self._record_not_found(handler)
        except ReimbursementStorageError:
            self._json(
                handler,
                503,
                {"error": "报销记录暂时无法删除，请稍后重试"},
            )

    def _reimbursement_generate(self, handler, user, _token) -> None:
        assert user is not None
        with self._upload_slots:
            self._reimbursement_generate_admitted(handler, user)

    def _reimbursement_generate_admitted(self, handler, user) -> None:
        body = self._multipart_body(handler)
        if body is None:
            return
        content_type = handler.headers.get("Content-Type", "")
        try:
            message = BytesParser(policy=policy.default).parsebytes(
                (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode(
                    "utf-8"
                )
                + body
            )
            if not message.is_multipart():
                raise ValueError("invalid multipart body")
            payload_raw: bytes | None = None
            screenshots: list[tuple[str, bytes]] = []
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == "payload" and payload_raw is None:
                    payload_raw = part.get_payload(decode=True) or b""
                elif name == "screenshots":
                    filename = part.get_filename()
                    if filename:
                        screenshots.append(
                            (
                                validate_image_filename(filename),
                                part.get_payload(decode=True) or b"",
                            )
                        )
            if payload_raw is None:
                raise ReimbursementValidationError("payload part is required")
            try:
                raw_payload = json.loads(payload_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ReimbursementValidationError("invalid JSON payload") from None
            validate_payload(
                raw_payload,
                traveler=user.real_name,
                department=user.department,
            )
        except (ReimbursementValidationError, ValueError, UnicodeError) as error:
            self._json(handler, 400, {"error": str(error) or "报销请求格式错误"})
            return

        try:
            with tempfile.TemporaryDirectory(prefix="reimbursement-upload-") as temp_name:
                image_paths = []
                for index, (filename, data) in enumerate(screenshots):
                    image_path = Path(temp_name) / f"{index:04d}-{filename}"
                    image_path.write_bytes(data)
                    image_paths.append(image_path)
                record = self.reimbursement_service.generate(
                    user, raw_payload, image_paths
                )
        except ReimbursementGenerationError as error:
            self._json(handler, 500, {"error": str(error)})
            return

        record_payload = self._record_payload(record)
        self._json(
            handler,
            200,
            {
                "record": record_payload,
                "xlsx_url": record_payload["xlsx_url"],
                "pdf_url": record_payload["pdf_url"],
                "xlsx_filename": record.display_name,
                "pdf_filename": record.display_name[:-5] + ".pdf",
            },
        )

    def _multipart_body(self, handler: BaseHTTPRequestHandler) -> bytes | None:
        length_headers = handler.headers.get_all("Content-Length", failobj=[])
        length_header = length_headers[0] if len(length_headers) == 1 else None
        if (
            length_header is None
            or not length_header.isascii()
            or not length_header.isdecimal()
            or len(length_header) > 20
            or handler.headers.get("Transfer-Encoding") is not None
        ):
            self._json(handler, 400, {"error": "请求体长度无效"})
            return None
        length = int(length_header, 10)
        if length > self.config.max_body_bytes:
            handler.close_connection = True
            self._json(handler, 413, {"error": "请求体过大"})
            return None
        content_type = handler.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
            self._json(handler, 415, {"error": "仅支持 multipart/form-data"})
            return None
        try:
            body = handler.read_request_body(length)
        except TimeoutError:
            handler.close_connection = True
            self._json(
                handler,
                408,
                {"error": "请求体读取超时"},
                headers={"Connection": "close"},
            )
            return None
        if len(body) != length:
            handler.close_connection = True
            self._json(
                handler,
                400,
                {"error": "请求体不完整"},
                headers={"Connection": "close"},
            )
            return None
        return body

    @staticmethod
    def _reimbursement_parts(handler: BaseHTTPRequestHandler) -> tuple[str, str] | None:
        path = WebApplication._decoded_path(handler.path)
        if path is None or not path.startswith(_REIMBURSEMENT_PREFIX):
            return None
        parts = path[len(_REIMBURSEMENT_PREFIX):].split("/")
        if len(parts) != 2 or not all(parts):
            return None
        try:
            record_uuid = UUID(parts[0])
        except ValueError:
            return None
        if record_uuid.version != 4 or str(record_uuid) != parts[0]:
            return None
        return parts[0], parts[1]

    @staticmethod
    def _record_payload(record: ReimbursementRecord) -> dict:
        root = f"/api/reimbursements/{record.id}"
        payload = {
            "id": record.id,
            "reimbursement_date": record.reimbursement_date,
            "display_name": record.display_name,
            "created_at": record.created_at,
            "deleted_at": record.deleted_at,
            "xlsx_url": root + "/xlsx",
            "pdf_url": root + "/pdf",
        }
        if record.deleted_at is not None:
            deleted_at = datetime.fromisoformat(record.deleted_at).astimezone(timezone.utc)
            payload["purge_at"] = (deleted_at + timedelta(days=30)).isoformat()
        return payload

    def _record_not_found(self, handler: BaseHTTPRequestHandler) -> None:
        self._json(handler, 404, {"error": "报销记录不存在"})

    def _not_found(self, handler, _user, _token) -> None:
        self._json(handler, 404, {"error": "请求路径不存在"})

    def _auth_page(self, handler: BaseHTTPRequestHandler, title: str, post_path: str) -> None:
        csrf_token = self.anonymous_csrf.issue("POST", post_path)
        markup = self._page_markup(title, csrf_token)
        self._html(handler, 200, markup)

    @staticmethod
    def _page_markup(title: str, csrf_token: str | None = None) -> bytes:
        csrf_attribute = "" if csrf_token is None else f' data-csrf="{html.escape(csrf_token)}"'
        markup = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title></head>"
            f"<body{csrf_attribute}><main><h1>{html.escape(title)}</h1></main></body></html>"
        )
        return markup.encode("utf-8")

    def _json_body(self, handler: BaseHTTPRequestHandler) -> dict | None:
        length_headers = handler.headers.get_all("Content-Length", failobj=[])
        length_header = length_headers[0] if len(length_headers) == 1 else None
        if (
            length_header is None
            or not length_header.isascii()
            or not length_header.isdecimal()
            or len(length_header) > 20
            or handler.headers.get("Transfer-Encoding") is not None
        ):
            self._json(handler, 400, {"error": "请求体长度无效"})
            return None
        length = int(length_header, 10)
        if length > self.config.max_body_bytes:
            handler.close_connection = True
            self._json(handler, 413, {"error": "请求体过大"})
            return None
        content_type = handler.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._json(handler, 415, {"error": "仅支持 application/json"})
            return None
        try:
            body = handler.read_request_body(length)
        except TimeoutError:
            handler.close_connection = True
            self._json(
                handler, 408, {"error": "请求体读取超时"},
                headers={"Connection": "close"},
            )
            return None
        if len(body) != length:
            handler.close_connection = True
            self._json(
                handler, 400, {"error": "请求体不完整"},
                headers={"Connection": "close"},
            )
            return None
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(handler, 400, {"error": "JSON 请求数据格式错误"})
            return None
        if not isinstance(value, dict):
            self._json(handler, 400, {"error": "JSON 请求数据必须是对象"})
            return None
        return value

    @staticmethod
    def _required_strings(payload: dict, *names: str) -> tuple[str, ...] | None:
        values = tuple(payload.get(name) for name in names)
        if not all(isinstance(value, str) for value in values):
            return None
        return values  # type: ignore[return-value]

    def _static(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        relative_text = path[len("/static/"):]
        relative = PurePosixPath(relative_text)
        if (
            not relative_text
            or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)
            or relative.suffix.lower() not in _STATIC_EXTENSIONS
            or "%" in relative_text
            or "\\" in relative_text
            or "\x00" in relative_text
        ):
            self._json(handler, 404, {"error": "请求路径不存在"})
            return
        static_root = self.config.static_dir.resolve()
        candidate = (static_root / Path(*relative.parts)).resolve()
        try:
            candidate.relative_to(static_root)
        except ValueError:
            self._json(handler, 404, {"error": "请求路径不存在"})
            return
        if not candidate.is_file():
            self._json(handler, 404, {"error": "请求路径不存在"})
            return
        self._file(handler, candidate)

    def _unauthenticated(self, handler, path: str, clear_cookie: bool) -> None:
        cookie = self._expired_cookie() if clear_cookie else None
        if self._is_api(path):
            self._json(handler, 401, {"error": "未登录或会话已失效"}, cookie=cookie)
        else:
            self._redirect(handler, "/login", cookie=cookie)

    def _forced_password_change(self, handler, path: str) -> None:
        if self._is_api(path):
            self._json(
                handler, 403,
                {"error": "请先修改临时密码", "next": "/change-password"},
            )
        else:
            self._redirect(handler, "/change-password")

    def _wrong_role(self, handler, path: str, user: AuthenticatedUser) -> None:
        if self._is_api(path):
            self._json(handler, 403, {"error": "没有权限执行此操作"})
        else:
            self._redirect(handler, self._home_for(user))

    def _login_failed(self, handler: BaseHTTPRequestHandler) -> None:
        self._json(handler, 401, {"error": "用户名或密码错误"})

    def _rate_limited(self, handler: BaseHTTPRequestHandler, key: Hashable) -> bool:
        retry_after = self.rate_limiter.reserve(key)
        return self._send_rate_limit_if_needed(handler, retry_after)

    def _rate_limited_many(
        self,
        handler: BaseHTTPRequestHandler,
        reservations: Iterable[tuple[Hashable, int]],
    ) -> bool:
        retry_after = self.rate_limiter.reserve_many(reservations)
        return self._send_rate_limit_if_needed(handler, retry_after)

    def _send_rate_limit_if_needed(
        self, handler: BaseHTTPRequestHandler, retry_after: int | None
    ) -> bool:
        if retry_after is None:
            return False
        self._json(
            handler,
            429,
            {"error": "请求过于频繁，请稍后重试"},
            headers={"Retry-After": str(retry_after)},
        )
        return True

    @staticmethod
    def _login_username_key(username: object) -> tuple[str, str]:
        try:
            _, canonical_key = canonicalize_username(username)
        except ValidationError:
            return _INVALID_LOGIN_USERNAME_KEY
        digest = hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()
        return ("valid-sha256", digest)

    @staticmethod
    def _client_ip(handler: BaseHTTPRequestHandler) -> str:
        return str(handler.client_address[0])

    @staticmethod
    def _decoded_path(request_target: str) -> str | None:
        try:
            raw_path = urlsplit(request_target).path
            decoded = unquote(raw_path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        if not decoded.startswith("/") or "\x00" in decoded:
            return None
        return decoded

    @staticmethod
    def _session_token(handler: BaseHTTPRequestHandler) -> str | None:
        raw_cookie = handler.headers.get("Cookie")
        if not raw_cookie:
            return None
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except cookies.CookieError:
            return None
        morsel = jar.get(_COOKIE_NAME)
        return morsel.value if morsel is not None else None

    @staticmethod
    def _is_api(path: str) -> bool:
        return path == "/api" or path.startswith("/api/")

    @staticmethod
    def _home_for(user: AuthenticatedUser) -> str:
        if user.must_change_password:
            return "/change-password"
        return "/admin" if user.role == "admin" else "/"

    @staticmethod
    def _home_for_user_snapshot(user) -> str:
        if user.must_change_password:
            return "/change-password"
        return "/admin" if user.role == "admin" else "/"

    def _session_cookie(self, token: str) -> str:
        attributes = [
            f"{_COOKIE_NAME}={token}", f"Max-Age={_COOKIE_MAX_AGE}", "Path=/",
            "HttpOnly", "SameSite=Lax",
        ]
        if self.config.cookie_secure:
            attributes.append("Secure")
        return "; ".join(attributes)

    def _expired_cookie(self) -> str:
        attributes = [
            f"{_COOKIE_NAME}=", "Max-Age=0", "Path=/", "HttpOnly", "SameSite=Lax",
        ]
        if self.config.cookie_secure:
            attributes.append("Secure")
        return "; ".join(attributes)

    def _method_not_allowed(self, handler: BaseHTTPRequestHandler, allow: str) -> None:
        self._json(handler, 405, {"error": "不支持此请求方法"}, headers={"Allow": allow})

    def _redirect(
        self, handler: BaseHTTPRequestHandler, location: str, cookie: str | None = None
    ) -> None:
        headers = {"Location": location}
        if cookie is not None:
            headers["Set-Cookie"] = cookie
        self._send(handler, 303, b"", "text/plain; charset=utf-8", headers, no_store=True)

    def _html(self, handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
        self._send(handler, status, body, "text/html; charset=utf-8", no_store=True)

    def _json(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        payload: dict,
        *,
        cookie: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        response_headers = dict(headers or {})
        if cookie is not None:
            response_headers["Set-Cookie"] = cookie
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(
            handler, status, body, "application/json; charset=utf-8", response_headers,
            no_store=True,
        )

    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
        *,
        no_store: bool = False,
    ) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("X-Content-Type-Options", "nosniff")
        if no_store:
            handler.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if body and handler.command != "HEAD":
            handler.wfile.write(body)

    def _file(
        self,
        handler: BaseHTTPRequestHandler,
        path: Path,
        content_type: str | None = None,
        *,
        no_store: bool = False,
    ) -> None:
        length = path.stat().st_size
        handler.send_response(200)
        handler.send_header(
            "Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        handler.send_header("Content-Length", str(length))
        handler.send_header("X-Content-Type-Options", "nosniff")
        if no_store:
            handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        with path.open("rb") as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
