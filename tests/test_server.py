import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib import request
from urllib.error import HTTPError

APP_DIR = Path(__file__).resolve().parents[1]
import sys

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app


VALID_PAYLOAD = {
    "date": "2026-09-04",
    "department": "技术部",
    "traveler": "张三",
    "reason": "客户拜访",
    "days": 2,
    "allowance": 50,
    "rows": [],
}


def multipart(parts, boundary="server-test"):
    body = bytearray()
    for name, value, filename, content_type in parts:
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body.extend(f"Content-Disposition: {disposition}\r\n".encode())
        if content_type:
            body.extend(f"Content-Type: {content_type}\r\n".encode())
        body.extend(b"\r\n")
        body.extend(value if isinstance(value, bytes) else value.encode())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "templates").mkdir()
        (self.root / "static").mkdir()
        (self.root / "templates" / "index.html").write_text("<html>form</html>", encoding="utf-8")
        (self.root / "static" / "app.js").write_text("ok", encoding="utf-8")
        self.output = self.root / "generated"
        (self.root / "template.xlsx").write_bytes(b"template")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "template_path": str(self.root / "template.xlsx"),
            "output_dir": "generated",
            "host": "127.0.0.1",
            "port": 0,
            "soffice_path": "",
            "templates_dir": str(self.root / "templates"),
            "static_dir": str(self.root / "static"),
        }), encoding="utf-8")
        self.server = app.create_server(self.config)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp.cleanup()

    def get(self, path):
        return request.urlopen(self.base + path)

    def test_root_and_allow_listed_static_asset_are_served(self):
        with self.get("/") as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"<html>form</html>", response.read())
        with self.get("/static/app.js") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")
        with self.assertRaises(HTTPError) as error:
            self.get("/static/../config.json")
        self.assertEqual(error.exception.code, 404)

    def test_generate_accepts_json_payload_and_screenshot_parts(self):
        body, content_type = multipart([
            ("payload", json.dumps(VALID_PAYLOAD), None, "application/json"),
            ("screenshots", b"PNGDATA", "route.png", "image/png"),
        ])
        result = mock.Mock(path=self.output / "result.xlsx")
        result.path.write_bytes(b"xlsx")
        pdf = self.output / "result.pdf"
        pdf.write_bytes(b"pdf")
        with mock.patch.object(app.generator, "generate_workbook", return_value=result) as generate, \
             mock.patch.object(app.office, "find_soffice", return_value=Path("/bin/true")), \
             mock.patch.object(app.office, "export_pdf", return_value=pdf) as export:
            req = request.Request(self.base + "/generate", data=body, method="POST", headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            })
            with request.urlopen(req) as response:
                payload = json.loads(response.read())
            self.assertEqual(response.status, 200)
        generate.assert_called_once()
        export.assert_called_once()
        self.assertEqual(payload["xlsx_url"], "/download/result.xlsx")
        self.assertEqual(payload["pdf_url"], "/download/result.pdf")

    def test_invalid_json_returns_400_json_error(self):
        body, content_type = multipart([("payload", "{bad", None, "application/json")])
        req = request.Request(self.base + "/generate", data=body, method="POST", headers={
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        })
        with self.assertRaises(HTTPError) as error:
            request.urlopen(req)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.loads(error.exception.read())["error"], "invalid JSON payload")

    def test_generate_encodes_reserved_filename_characters_in_download_urls(self):
        workbook = self.output / "2026-09-04-A?#%-差旅报销单.xlsx"
        pdf = self.output / "2026-09-04-A?#%-差旅报销单.pdf"
        workbook.write_bytes(b"xlsx")
        pdf.write_bytes(b"pdf")
        result = mock.Mock(path=workbook)
        body, content_type = multipart([("payload", json.dumps(VALID_PAYLOAD), None, "application/json")])
        with mock.patch.object(app.generator, "generate_workbook", return_value=result), \
             mock.patch.object(app.office, "find_soffice", return_value=Path("/bin/true")), \
             mock.patch.object(app.office, "export_pdf", return_value=pdf):
            req = request.Request(self.base + "/generate", data=body, method="POST", headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            })
            with request.urlopen(req) as response:
                generated = json.loads(response.read())
        self.assertEqual(generated["xlsx_filename"], workbook.name)
        self.assertEqual(generated["pdf_filename"], pdf.name)
        self.assertIn("%3F", generated["xlsx_url"])
        self.assertIn("%23", generated["xlsx_url"])
        self.assertIn("%25", generated["xlsx_url"])
        with request.urlopen(self.base + generated["xlsx_url"]) as response:
            self.assertEqual(response.read(), b"xlsx")
        with request.urlopen(self.base + generated["pdf_url"]) as response:
            self.assertEqual(response.read(), b"pdf")

    def test_export_failure_removes_workbook_and_returns_500(self):
        workbook = self.output / "partial.xlsx"
        workbook.parent.mkdir(parents=True, exist_ok=True)
        workbook.write_bytes(b"xlsx")
        result = mock.Mock(path=workbook)
        body, content_type = multipart([("payload", json.dumps(VALID_PAYLOAD), None, "application/json")])
        with mock.patch.object(app.generator, "generate_workbook", return_value=result), \
             mock.patch.object(app.office, "find_soffice", return_value=Path("/bin/true")), \
             mock.patch.object(app.office, "export_pdf", side_effect=RuntimeError("conversion failed")):
            req = request.Request(self.base + "/generate", data=body, method="POST", headers={
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
            })
            with self.assertRaises(HTTPError) as error:
                request.urlopen(req)
        self.assertEqual(error.exception.code, 500)
        self.assertFalse(workbook.exists())
        self.assertEqual(list(self.output.glob("*.pdf")), [])

    def test_download_rejects_traversal_and_only_serves_generated_files(self):
        self.output.mkdir(parents=True, exist_ok=True)
        (self.output / "ok.xlsx").write_bytes(b"xlsx")
        with self.get("/download/ok.xlsx") as response:
            self.assertEqual(response.read(), b"xlsx")
        with self.assertRaises(HTTPError) as error:
            self.get("/download/../config.json")
        self.assertEqual(error.exception.code, 404)

    def test_body_size_limit_is_checked_before_multipart_parsing(self):
        self.server.max_body_bytes = 8
        body = b"0123456789"
        req = request.Request(self.base + "/generate", data=body, method="POST", headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(len(body)),
        })
        with self.assertRaises(HTTPError) as error:
            request.urlopen(req)
        self.assertEqual(error.exception.code, 400)

    def test_create_server_honors_explicit_lan_bind_host(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.config.write_text(json.dumps({
            "template_path": str(self.root / "template.xlsx"),
            "output_dir": "generated",
            "host": "0.0.0.0",
            "port": 0,
            "soffice_path": "",
            "templates_dir": str(self.root / "templates"),
            "static_dir": str(self.root / "static"),
        }), encoding="utf-8")
        server = app.create_server(self.config)
        try:
            self.assertEqual(server.server_address[0], "0.0.0.0")
        finally:
            server.server_close()


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
