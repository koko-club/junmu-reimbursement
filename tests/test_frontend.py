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

    def test_sidebar_shows_current_user_above_logout(self):
        for page in ("index.html", "history.html", "admin.html"):
            markup = self.read_template(page)
            self.assertIn('class="sidebar-account"', markup)
            self.assertIn('id="current-user"', markup)
            self.assertIn('当前登录：', markup)
            self.assertLess(markup.index('class="sidebar-account"'), markup.index('id="logout"'))
        common = (ROOT / "static" / "common.js").read_text(encoding="utf-8")
        self.assertIn("document.getElementById('current-user')", common)
        self.assertIn("const displayName = user && user.username", common)
        self.assertNotIn("user.real_name ||", common)

    def test_reimbursement_workspace_has_history_stats_cards(self):
        markup = self.read_template("index.html")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="stats-grid"', markup)
        expected_cards = ("month-count", "year-count", "year-amount")
        positions = [markup.index(f'id="{card}"') for card in expected_cards]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(markup.index('id="month-count"'), markup.index('id="basic-heading"'))
        self.assertIn("/api/reimbursements/stats", script)
        self.assertIn("Intl.NumberFormat('zh-CN'", script)
        self.assertIn("¥", script)
        self.assertIn("visibilitychange", script)
        self.assertIn("storage", script)
        self.assertGreaterEqual(script.count("loadStats();"), 3)
        self.assertIn("await loadStats();", script)

    def test_mileage_screenshot_entry_opens_development_dialog(self):
        markup = self.read_template("index.html")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="mileage-development-trigger"', markup)
        self.assertIn('id="mileage-development-dialog"', markup)
        self.assertIn('src="/static/mileage-development-qr.jpg"', markup)
        self.assertIn('功能开发中，扫码助力开发。', markup)
        self.assertIn("mileageDialog.showModal()", script)
        self.assertIn("event.target === mileageDialog", script)
        self.assertRegex(
            script,
            re.compile(
                r"/\* Original mileage screenshot selection.*?"
                r"fileInput\.addEventListener\('change'.*?\*/",
                re.DOTALL,
            ),
        )
        self.assertIn(".mileage-development-dialog", styles)
        self.assertTrue((ROOT / "static" / "mileage-development-qr.jpg").is_file())

    def test_stats_cards_have_informative_zero_state(self):
        markup = self.read_template("index.html")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="month-count" class="stat-value" aria-live="polite">0 笔', markup)
        self.assertIn('id="year-count" class="stat-value" aria-live="polite">0 笔', markup)
        self.assertIn('id="year-amount" class="stat-value" aria-live="polite">¥0.00', markup)
        self.assertIn("monthCount.textContent = '0 笔'", script)
        self.assertIn("yearCount.textContent = '0 笔'", script)
        self.assertIn("yearAmount.textContent = '¥0.00'", script)

    def test_generation_uses_authenticated_api_and_pdf_new_tab(self):
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/reimbursements/generate", script)
        self.assertIn("apiFetch", script)
        self.assertIn("link.target = '_blank'", script)
        self.assertIn("link.rel = 'noopener'", script)

    def test_generation_success_uses_check_icon_and_button_downloads(self):
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        icons = (ROOT / "static" / "icons.svg").read_text(encoding="utf-8")
        self.assertIn("result-success-icon", script)
        self.assertIn("/static/icons.svg#check", script)
        self.assertNotIn("strong.textContent = '生成成功'", script)
        self.assertIn("link.className = 'table-action-button'", script)
        self.assertIn('<symbol id="check"', icons)
        self.assertRegex(styles, r"\.result-success-icon\{[^}]*color:#4f7d63")

    def test_history_has_active_and_trash_actions(self):
        markup = self.read_template("history.html")
        script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        self.assertNotIn('<th>文件名称</th>', markup)
        self.assertIn('<th>出差事由</th>', markup)
        self.assertIn('<th>报销金额</th>', markup)
        self.assertLess(markup.index('<th>出差事由</th>'), markup.index('<th>报销日期</th>'))
        self.assertLess(markup.index('<th>报销日期</th>'), markup.index('<th>报销金额</th>'))
        self.assertLess(markup.index('<th>报销金额</th>'), markup.index('<th>生成时间</th>'))
        self.assertIn("record.reason", script)
        self.assertIn("record.reimbursement_amount", script)
        self.assertNotIn("text('td', record.display_name)", script)
        self.assertIn("xlsx.download = record.display_name", script)
        self.assertIn("tr.dataset.displayName = record.display_name || ''", script)
        self.assertIn("name: tr.dataset.displayName || ''", script)
        self.assertIn('data-view="active"', markup)
        self.assertIn('data-view="trash"', markup)
        self.assertIn("window.open(pdfUrl, '_blank', 'noopener')", script)
        self.assertIn("/restore", script)
        self.assertIn("/purge", script)
        self.assertIn("Intl.DateTimeFormat('zh-CN'", script)
        self.assertIn("textContent", script)
        self.assertIn("purge_at", script)
        self.assertIn("<dialog", markup)

    def test_history_action_cell_preserves_table_layout(self):
        script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("const actionItems = document.createElement('div')", script)
        self.assertIn("actionItems.className = 'table-action-items'", script)
        self.assertIn("actions.append(actionItems)", script)
        self.assertRegex(styles, r"\.table-actions\{display:table-cell;vertical-align:middle\}")
        self.assertRegex(styles, r"\.table-action-items\{[^}]*display:flex")

    def test_history_excel_download_uses_button_presentation(self):
        script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("xlsx.className = 'table-action-button'", script)
        self.assertRegex(styles, r"\.table-action-button\{[^}]*display:inline-flex")
        self.assertRegex(styles, r"\.table-action-button\{[^}]*background:var\(--blue\)")

    def test_history_actions_stay_on_one_row_with_destructive_trash_button(self):
        script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("actionButton('移入回收站', 'trash', 'danger')", script)
        self.assertRegex(styles, r"\.table-action-items\{[^}]*flex-wrap:nowrap")
        self.assertRegex(styles, r"\.table-actions button\.danger\{[^}]*background:#8f3f3b")

    def test_trash_status_column_is_wider_without_changing_history_columns(self):
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        self.assertRegex(styles, r'body\[data-scope="trash"\].*\.history-col-status\{width:20%;white-space:nowrap\}')
        self.assertRegex(styles, r'body\[data-scope="trash"\].*\.history-col-actions\{width:20%\}')
        self.assertIn('body[data-scope="trash"] .history-table th:nth-child(5)', styles)
        self.assertIn('body[data-scope="trash"] .history-table td:nth-child(5)', styles)
        self.assertIn("actionButton('永久删除', 'purge', 'danger')", script)

    def test_history_mutations_notify_reimbursement_stats(self):
        script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        self.assertIn("localStorage.setItem('reimbursement-history-updated'", script)
        self.assertIn("notifyHistoryUpdated()", script)

    def test_history_columns_reserve_space_for_reason_and_actions(self):
        markup = self.read_template("history.html")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('<colgroup class="history-colgroup">', markup)
        expected_columns = (
            "history-col-reason",
            "history-col-date",
            "history-col-amount",
            "history-col-created",
            "history-col-status",
            "history-col-actions",
        )
        positions = [markup.index(f'class="{column}"') for column in expected_columns]
        self.assertEqual(positions, sorted(positions))
        self.assertRegex(styles, r"\.history-table\{[^}]*min-width:1200px")
        self.assertRegex(styles, r"\.history-col-reason\{[^}]*width:28%")
        self.assertRegex(styles, r"\.history-col-amount\{[^}]*width:9%")
        self.assertRegex(styles, r"\.history-col-status\{[^}]*width:7%")
        self.assertRegex(styles, r"\.history-col-actions\{[^}]*width:28%")

    def test_admin_has_no_reimbursement_access(self):
        markup = self.read_template("admin.html")
        script = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
        combined = markup + script
        self.assertIn("待审批", combined)
        self.assertIn("用户管理", combined)
        self.assertIn("reset-password", combined)
        self.assertNotIn("/api/reimbursements", combined)
        self.assertIn("profile", script)
        self.assertIn("status", script)
        self.assertIn("confirm", script)
        self.assertIn("textContent", script)
        self.assertIn("<dialog", markup)


if __name__ == "__main__":
    unittest.main()
