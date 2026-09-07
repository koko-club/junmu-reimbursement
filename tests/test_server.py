from pathlib import Path
import unittest

from tests.http_helpers import RunningApp


APP_DIR = Path(__file__).resolve().parents[1]


class ServerTest(unittest.TestCase):
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
