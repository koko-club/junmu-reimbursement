from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTest(unittest.TestCase):
    def read_template(self, name: str) -> str:
        return (ROOT / "templates" / name).read_text(encoding="utf-8")

    def test_auth_pages_have_expected_fields_and_safe_password_defaults(self):
        expected = {
            "setup.html": ("username", "password", "confirm-password", "real-name", "department"),
            "register.html": ("username", "password", "confirm-password", "real-name", "department"),
            "login.html": ("username", "password"),
            "change-password.html": ("current-password", "new-password", "confirm-password"),
        }
        for page, fields in expected.items():
            markup = self.read_template(page)
            for field in fields:
                self.assertRegex(markup, rf'id="{re.escape(field)}"')
            self.assertNotRegex(markup, r'type="password"[^>]+value=')
            self.assertIn('autocomplete=', markup)
            self.assertIn('aria-live="polite"', markup)

    def test_shared_scripts_handle_csrf_and_expired_sessions(self):
        common = (ROOT / "static" / "common.js").read_text(encoding="utf-8")
        self.assertIn("X-CSRF-Token", common)
        self.assertIn("credentials: 'same-origin'", common)
        self.assertIn("response.status === 401", common)
        self.assertIn("window.location.assign('/login')", common)
        auth = (ROOT / "static" / "auth.js").read_text(encoding="utf-8")
        self.assertIn("confirm_password", auth)
        self.assertIn("textContent", auth)

    def test_templates_load_shared_assets_and_bound_csrf_marker(self):
        for page in ("setup.html", "register.html", "login.html"):
            markup = self.read_template(page)
            self.assertIn('/static/styles.css', markup)
            self.assertIn('/static/common.js', markup)
            self.assertIn('/static/auth.js', markup)
            self.assertIn('data-csrf="__CSRF_TOKEN__"', markup)
        change = self.read_template("change-password.html")
        self.assertIn('data-csrf="__CSRF_TOKEN__"', change)

    def test_reimbursement_workspace_has_sidebar_and_locked_profile(self):
        markup = self.read_template("index.html")
        for href in ('href="/"', 'href="/history"', 'href="/trash"'):
            self.assertIn(href, markup)
        self.assertRegex(markup, r'id="traveler"[^>]+readonly')
        self.assertRegex(markup, r'id="department"[^>]+readonly')
        self.assertIn('aria-controls="app-sidebar"', markup)
        self.assertEqual(markup.count('class="detail-row"'), 11)

    def test_generation_uses_authenticated_api_and_pdf_new_tab(self):
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/reimbursements/generate", script)
        self.assertIn("apiFetch", script)
        self.assertIn("link.target = '_blank'", script)
        self.assertIn("link.rel = 'noopener'", script)


if __name__ == "__main__":
    unittest.main()
