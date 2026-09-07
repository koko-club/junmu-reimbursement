"""Local-only HTTP service for generating reimbursement workbooks and PDFs."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
import json
import mimetypes
from pathlib import Path
import socket
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

try:  # Package invocation.
    from .validation import ValidationError, validate_image_filename, validate_payload
except ImportError:  # Direct invocation from the app directory.
    from validation import ValidationError, validate_image_filename, validate_payload  # type: ignore

try:
    from . import generator, office
except ImportError:  # Direct invocation from the app directory.
    try:
        import generator  # type: ignore
        import office  # type: ignore
    except ImportError:
        generator = None  # type: ignore
        office = None  # type: ignore


DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_STATIC_EXTENSIONS = {
    ".css", ".js", ".html", ".ico", ".png", ".jpg", ".jpeg", ".svg", ".webp",
    ".woff", ".woff2", ".ttf",
}


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _path_from_config(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def _load_config(config_path: Path) -> dict:
    path = Path(config_path)
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    base = path.parent
    config = dict(config)
    config["template_path"] = _path_from_config(str(config.get("template_path", "")), base)
    config["output_dir"] = _path_from_config(str(config.get("output_dir", "generated")), base)
    config["host"] = str(config.get("host", "127.0.0.1")).strip() or "127.0.0.1"
    config["port"] = int(config.get("port", 0))
    config["soffice_path"] = str(config.get("soffice_path", ""))
    config["max_body_bytes"] = int(config.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES))
    if config["max_body_bytes"] <= 0:
        raise ValueError("max_body_bytes must be positive")
    return config


def _extract_workbook_path(result) -> Path:
    if isinstance(result, dict):
        for key in ("path", "xlsx_path", "workbook_path", "output_path"):
            if key in result:
                return Path(result[key])
    for key in ("path", "xlsx_path", "workbook_path", "output_path"):
        value = getattr(result, key, None)
        if value is not None:
            return Path(value)
    return Path(result)


def _within_directory(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _download_url(path: Path) -> str:
    """Encode a generated filename as one URL path segment without renaming it."""
    return "/download/" + quote(Path(path).name, safe="")


def create_server(config_path: Path) -> ThreadingHTTPServer:
    """Create a configured server; use ``0.0.0.0`` for trusted-LAN access."""
    config = _load_config(Path(config_path))
    host = config["host"]
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    app_dir = Path(__file__).resolve().parent
    templates_dir = _path_from_config(str(config.get("templates_dir", app_dir / "templates")), Path(config_path).parent)
    static_dir = _path_from_config(str(config.get("static_dir", app_dir / "static")), Path(config_path).parent)
    max_body_bytes = config["max_body_bytes"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "ReimbursementTool/1.0"

        def _send_json(self, status: int, payload: dict) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str | None = None) -> None:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            route = unquote(parsed.path)
            if route == "/":
                index = templates_dir / "index.html"
                if not index.is_file():
                    self._send_json(500, {"error": "template is missing"})
                    return
                self._send_file(index, "text/html; charset=utf-8")
                return
            if route.startswith("/static/"):
                relative = route[len("/static/"):]
                path = (static_dir / relative).resolve()
                if (not relative or Path(relative).suffix.lower() not in ALLOWED_STATIC_EXTENSIONS
                        or any(part in ("", ".", "..") for part in Path(relative).parts)):
                    self._send_json(404, {"error": "not found"})
                    return
                if not _within_directory(path, static_dir) or not path.is_file():
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_file(path)
                return
            if route.startswith("/download/"):
                filename = route[len("/download/"):]
                if (not filename or Path(filename).name != filename or "/" in filename or "\\" in filename
                        or Path(filename).suffix.lower() not in {".xlsx", ".pdf"}):
                    self._send_json(404, {"error": "not found"})
                    return
                path = (output_dir / filename).resolve()
                if not _within_directory(path, output_dir) or not path.is_file():
                    self._send_json(404, {"error": "not found"})
                    return
                self._send_file(path)
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != "/generate":
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > self.server.max_body_bytes:
                self._send_json(400, {"error": "request body is too large or missing length"})
                return
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("multipart/form-data"):
                self._send_json(400, {"error": "multipart/form-data is required"})
                return
            try:
                body = self.rfile.read(length)
                message = BytesParser(policy=policy.default).parsebytes(
                    (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body
                )
                if not message.is_multipart():
                    raise ValueError("invalid multipart body")
                payload_raw = None
                screenshots: list[tuple[str, bytes]] = []
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name == "payload" and payload_raw is None:
                        payload_raw = part.get_payload(decode=True) or b""
                    elif name == "screenshots":
                        filename = part.get_filename()
                        if filename:
                            validate_image_filename(filename)
                            screenshots.append((filename, part.get_payload(decode=True) or b""))
                if payload_raw is None:
                    raise ValidationError("payload part is required")
                try:
                    raw_payload = json.loads(payload_raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("invalid JSON payload") from exc
                payload = validate_payload(raw_payload)
            except ValidationError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                self._send_json(400, {"error": f"invalid multipart request: {exc}"})
                return

            if not Path(config["template_path"]).is_file() or generator is None or office is None:
                self._send_json(500, {"error": "generation dependencies are unavailable"})
                return
            workbook_path = None
            pdf_path = None
            try:
                soffice_path = office.find_soffice(config["soffice_path"])
                with tempfile.TemporaryDirectory(prefix="reimbursement-upload-") as temp_name:
                    image_paths = []
                    for index, (filename, data) in enumerate(screenshots):
                        image_path = Path(temp_name) / f"{index:04d}-{filename}"
                        image_path.write_bytes(data)
                        image_paths.append(image_path)
                    result = generator.generate_workbook(
                        Path(config["template_path"]), output_dir, payload, image_paths
                    )
                    workbook_path = _extract_workbook_path(result)
                    pdf_path = office.export_pdf(workbook_path, output_dir, soffice_path)
                if not _within_directory(workbook_path, output_dir) or not _within_directory(Path(pdf_path), output_dir):
                    raise RuntimeError("generator returned an unsafe output path")
                self._send_json(200, {
                    "xlsx_url": _download_url(workbook_path),
                    "pdf_url": _download_url(Path(pdf_path)),
                    "xlsx_filename": workbook_path.name,
                    "pdf_filename": Path(pdf_path).name,
                })
            except Exception as exc:
                for path in (workbook_path, pdf_path):
                    if path is not None:
                        try:
                            candidate = Path(path)
                            if _within_directory(candidate, output_dir):
                                candidate.unlink(missing_ok=True)
                        except OSError:
                            pass
                self._send_json(500, {"error": str(exc) or "generation failed"})

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer((host, config["port"]), Handler)
    server.config = config
    server.max_body_bytes = max_body_bytes
    return server


def main() -> None:
    config_path = Path(__file__).resolve().with_name("config.json")
    server = create_server(config_path)
    host, port = server.server_address[:2]
    print(f"http://127.0.0.1:{port}" if host in {"0.0.0.0", "::"} else f"http://{host}:{port}", flush=True)
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
