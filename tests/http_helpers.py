from __future__ import annotations

from dataclasses import dataclass
import http.client
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import tempfile
import threading
from urllib import request
from urllib.error import HTTPError
from urllib.parse import urlsplit

import app


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: object
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


class HttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.cookies = CookieJar()
        cookie_handler = request.HTTPCookieProcessor(self.cookies)
        self._following = request.build_opener(cookie_handler)
        self._not_following = request.build_opener(cookie_handler, _NoRedirect())

    def get(self, path: str, *, follow_redirects: bool = True) -> HttpResponse:
        return self.request("GET", path, follow_redirects=follow_redirects)

    def post_json(
        self,
        path: str,
        payload: object,
        *,
        csrf: bool | str = True,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        headers = {"Content-Type": "application/json"}
        if csrf is True:
            headers["X-CSRF-Token"] = self.csrf_for(path)
        elif isinstance(csrf, str):
            headers["X-CSRF-Token"] = csrf
        return self.request(
            "POST",
            path,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            follow_redirects=follow_redirects,
        )

    def csrf_for(self, post_path: str) -> str:
        anonymous_pages = {
            "/api/setup": "/setup",
            "/api/login": "/login",
            "/api/register": "/register",
        }
        if post_path in anonymous_pages:
            page = self.get(anonymous_pages[post_path])
            match = re.search(r'data-csrf="([^"]+)"', page.text)
            if match is None:
                raise AssertionError(f"CSRF token missing from {anonymous_pages[post_path]}")
            return match.group(1)
        session = self.get("/api/session")
        return session.json()["csrf_token"]

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> HttpResponse:
        req = request.Request(
            self.base_url + path,
            data=body,
            headers=headers or {},
            method=method,
        )
        opener = self._following if follow_redirects else self._not_following
        try:
            response = opener.open(req)
        except HTTPError as error:
            response = error
        with response:
            return HttpResponse(response.status, response.headers, response.read())

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: list[tuple[str, str]] | None = None,
    ) -> HttpResponse:
        parsed = urlsplit(self.base_url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        connection.putrequest(method, path, skip_accept_encoding=True)
        request_headers = list(headers or [])
        if not any(name.lower() == "cookie" for name, _value in request_headers):
            cookie_header = "; ".join(
                f"{cookie.name}={cookie.value}"
                for cookie in self.cookies
                if not cookie.secure
            )
            if cookie_header:
                request_headers.append(("Cookie", cookie_header))
        for name, value in request_headers:
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        result = HttpResponse(response.status, response.headers, response.read())
        connection.close()
        return result


class RunningApp:
    def __init__(self, *, cookie_secure: bool = False, max_body_bytes: int = 1024 * 1024):
        self.cookie_secure = cookie_secure
        self.max_body_bytes = max_body_bytes
        self._stopped = False

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.templates_dir = self.root / "templates"
        self.static_dir = self.root / "static"
        self.data_dir = self.root / "data"
        self.templates_dir.mkdir()
        self.static_dir.mkdir()
        (self.templates_dir / "index.html").write_text(
            "<!doctype html><html><body>reimbursement form</body></html>", encoding="utf-8"
        )
        (self.static_dir / "app.js").write_text("window.appLoaded = true;", encoding="utf-8")
        (self.root / "template.xlsx").write_bytes(b"template")
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "template_path": "template.xlsx",
                    "data_dir": "data",
                    "templates_dir": "templates",
                    "static_dir": "static",
                    "host": "127.0.0.1",
                    "port": 0,
                    "max_body_bytes": self.max_body_bytes,
                    "cookie_secure": self.cookie_secure,
                }
            ),
            encoding="utf-8",
        )
        self.server = app.create_server(self.config_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = self.new_client()
        return self

    @property
    def users(self):
        return self.server.application.user_service

    def new_client(self) -> HttpClient:
        return HttpClient(self.base_url)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise AssertionError("HTTP server thread did not stop")

    def __exit__(self, exc_type, exc, traceback):
        try:
            self.stop()
        finally:
            self._temporary.cleanup()
