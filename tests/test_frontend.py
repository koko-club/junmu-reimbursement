from pathlib import Path
import hashlib
import re
import unittest

from web import WebApplication


ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTest(unittest.TestCase):
    def read_template(self, name: str) -> str:
        return (ROOT / "templates" / name).read_text(encoding="utf-8")

    def test_all_pages_use_site_title_and_favicon(self):
        title_pattern = re.compile(
            r"<title(?:\s+data-page-title)?[^>]*>在线报销系统</title>"
        )
        favicon = '<link rel="icon" type="image/png" href="/static/favicon.png">'
        for page in (
            "setup.html",
            "register.html",
            "login.html",
            "change-password.html",
            "index.html",
            "history.html",
            "admin.html",
        ):
            with self.subTest(page=page):
                markup = self.read_template(page)
                self.assertRegex(markup, title_pattern)
                self.assertIn(favicon, markup)

        favicon_path = ROOT / "static" / "favicon.png"
        self.assertTrue(favicon_path.is_file())
        self.assertEqual(favicon_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(
            hashlib.sha256(favicon_path.read_bytes()).hexdigest(),
            "0e11960647ac87168036ad7ad78e8e7a10aaf40b659d7fa5d7fe7f1d9f7c6015",
        )
        history = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        self.assertNotRegex(history, r"document\s*\.\s*title\s*=\s*['\"]回收站['\"]")

    def test_fallback_page_markup_uses_site_title_and_favicon(self):
        markup = WebApplication._page_markup("内部错误").decode("utf-8")
        self.assertIn("<title>在线报销系统</title>", markup)
        self.assertIn(
            '<link rel="icon" type="image/png" href="/static/favicon.png">',
            markup,
        )
        self.assertIn("<h1>内部错误</h1>", markup)

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

    def test_login_page_omits_travel_reimbursement_eyebrow(self):
        markup = self.read_template("login.html")
        self.assertNotIn("差旅报销系统", markup)

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

    def test_all_pages_load_component_visual_layers_without_changing_generation_contract(self):
        for page in ("setup.html", "register.html", "login.html", "change-password.html", "index.html", "history.html", "admin.html"):
            markup = self.read_template(page)
            self.assertIn('/static/ui-tokens.css', markup)
            self.assertIn('/static/ui-components.css', markup)
        index = self.read_template("index.html")
        self.assertIn('id="reimbursement-form"', index)
        self.assertIn('id="submit"', index)
        self.assertIn('生成 Excel + PDF', index)
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('/api/reimbursements/generate', app)
        self.assertIn("link.target = '_blank'", app)

    def test_component_visual_layers_define_low_saturation_tokens_and_shared_surfaces(self):
        tokens = (ROOT / "static" / "ui-tokens.css").read_text(encoding="utf-8")
        components = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertIn('--ui-bg-gradient', tokens)
        self.assertIn('--ui-brand-blue', tokens)
        self.assertIn('--ui-accent-rose', tokens)
        self.assertIn('.app-sidebar', components)
        self.assertIn('.form-section', components)
        self.assertIn('.auth-panel', components)
        self.assertIn('.stat-card', components)
        self.assertIn('.detail-table', components)
        self.assertIn('.history-table', components)
        self.assertIn('.admin-table', components)

    def test_all_buttons_follow_shared_capsule_visual_contract(self):
        components = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        tokens = (ROOT / "static" / "ui-tokens.css").read_text(encoding="utf-8")
        # The approved visual language uses black primary and white secondary
        # capsule buttons, with compact table actions and a red danger variant.
        self.assertIn('--ui-button-primary: #1d1d1f', tokens)
        self.assertRegex(components, r"button[^{]*\{[^}]*border-radius:\s*var\(--ui-radius-pill\)")
        self.assertRegex(components, r"button[^{]*\{[^}]*background:\s*var\(--ui-button-primary\)")
        self.assertRegex(components, r"button\.secondary[^,{]*,[^\{]*\{[^}]*background:\s*(?:#fff|var\(--ui-button-secondary\))")
        self.assertRegex(components, r"button\.secondary[^,{]*,[^\{]*\{[^}]*color:\s*var\(--ui-text\)")
        self.assertIn('button:hover', components)
        self.assertIn('button:active', components)
        self.assertIn('button:focus-visible', components)
        self.assertIn('button:disabled', components)
        self.assertRegex(components, r"\.table-action-button\s*\{[^}]*border-radius:\s*var\(--ui-radius-pill\)")
        self.assertRegex(components, r"\.table-action-button\s*\{[^}]*min-height:\s*34px")
        self.assertIn('.table-action-button.secondary', components)
        self.assertIn('a.table-action-button:not(.secondary)', components)
        self.assertIn('a.table-action-button.secondary', components)
        self.assertIn('button.secondary:focus-visible', components)
        self.assertRegex(components, r"\.table-actions[^\{]*button\.danger[^\{]*\{[^}]*background:\s*var\(--ui-danger\)")
        self.assertRegex(components, r"button\.danger[^\{]*\{[^}]*background:\s*var\(--ui-danger\)")
        self.assertIn('class="danger" data-confirm-purge', self.read_template("history.html"))

        # Dynamically-created controls opt into the same visual classes.
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        history = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        admin = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
        self.assertIn("remove.className = 'remove-file secondary'", app)
        self.assertIn("xlsx.className = 'table-action-button secondary'", history)
        self.assertIn("button.className = 'table-action-button' + (className ? ' ' + className : '')", history)
        self.assertIn("actionButton('打开 PDF', 'pdf', 'secondary')", history)
        self.assertIn("actionButton('恢复', 'restore', 'secondary')", history)
        self.assertIn('value="close" class="secondary"', self.read_template("admin.html"))
        self.assertIn("node.className = 'table-action-button' + (variant ? ' ' + variant : '')", admin)
        self.assertIn("button('拒绝', 'reject', user.id, 'danger')", admin)
        self.assertIn("button('编辑资料', 'profile', user.id, 'secondary')", admin)
        self.assertIn("button('重置密码', 'reset-password', user.id, 'secondary')", admin)
        self.assertIn("button(user.status === 'active' ? '停用' : '启用', 'status', user.id, user.status === 'active' ? 'danger' : '')", admin)

    def test_sidebar_navigation_uses_primary_and_secondary_capsules(self):
        components = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertRegex(components, r"\.app-sidebar nav a\s*\{[^}]*border-radius:\s*var\(--ui-radius-pill\)")
        self.assertRegex(components, r"\.app-sidebar nav a\[aria-current=\"page\"\][^\{]*\{[^}]*background:\s*var\(--ui-button-primary\)")
        self.assertRegex(components, r"\.app-sidebar nav a\[aria-current=\"page\"\][^\{]*\{[^}]*color:\s*#fff")
        self.assertRegex(components, r"\.app-sidebar nav a:not\(\[aria-current=\"page\"\]\)[^\{]*\{[^}]*background:\s*var\(--ui-button-secondary\)")
        self.assertRegex(components, r"\.app-sidebar nav a:not\(\[aria-current=\"page\"\]\)[^\{]*\{[^}]*color:\s*var\(--ui-text\)")
        self.assertRegex(components, r"\.app-sidebar > button\.secondary\s*\{[^}]*color:\s*var\(--ui-text\)")

    def test_history_navigation_sets_exact_current_page_state(self):
        script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")

        self.assertIn(
            "node.setAttribute('aria-current', 'page')",
            script,
        )
        self.assertIn("node.removeAttribute('aria-current')", script)
        self.assertNotIn("toggleAttribute('aria-current'", script)

    def test_reimbursement_workspace_has_sidebar_and_locked_profile(self):
        markup = self.read_template("index.html")
        for href in ('href="/"', 'href="/history"', 'href="/trash"'):
            self.assertIn(href, markup)
        self.assertRegex(markup, r'id="traveler"[^>]+readonly')
        self.assertRegex(markup, r'id="department"[^>]+readonly')
        self.assertIn('aria-controls="app-sidebar"', markup)
        self.assertEqual(markup.count('class="detail-row"'), 11)

    def test_detail_table_gives_date_room_and_keeps_sequence_compact(self):
        markup = self.read_template("index.html")
        styles = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertRegex(
            markup,
            re.compile(
                r'<colgroup[^>]*class="detail-colgroup"[^>]*>.*?'
                r'<col[^>]*class="detail-col-index"[^>]*>.*?'
                r'<col[^>]*class="detail-col-date"[^>]*>.*?</colgroup>',
                re.DOTALL,
            ),
        )
        self.assertRegex(styles, r'\.detail-col-index\s*\{[^}]*width:\s*64px')
        self.assertRegex(styles, r'\.detail-col-date\s*\{[^}]*width:\s*180px')

    def test_detail_rows_use_subtle_dividers_and_regular_sequence_numbers(self):
        styles = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertRegex(
            styles,
            r'\.detail-table tbody td,\s*\.detail-table tbody th\s*\{[^}]*'
            r'border-bottom-color:\s*rgba\(105,\s*128,\s*149,\s*0\.12\)',
        )
        self.assertRegex(
            styles,
            r'\.detail-table tbody th\s*\{[^}]*font-weight:\s*400',
        )

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

    def test_workspace_pages_contain_version_marker_and_sidebar_slot(self):
        for page in ("index.html", "history.html", "admin.html"):
            with self.subTest(page=page):
                markup = self.read_template(page)
                self.assertIn('class="sidebar-version"', markup)
                self.assertIn("版本：", markup)
                self.assertIn("__APP_VERSION__", markup)

    def test_sidebar_account_and_version_share_horizontal_padding(self):
        components = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertRegex(
            components,
            r"\.sidebar-account\s*\{[^}]*padding:\s*13px\s+14px\s+0;",
        )
        self.assertRegex(
            components,
            r"\.sidebar-version\s*\{[^}]*padding:\s*0\s+14px;",
        )

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

    def test_year_reimbursement_amount_uses_black_text(self):
        components = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertRegex(
            components,
            r"\.stat-card:nth-child\(3\) \.stat-value\s*\{[^}]*color:\s*#1d1d1f",
        )

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
        self.assertNotIn('id="mileage-development-close"', markup)
        self.assertNotIn("mileageClose", script)
        self.assertNotIn("mileage-development-close", styles)
        self.assertRegex(styles, r"\.mileage-development-dialog\{[^}]*border:0")
        self.assertRegex(styles, r"\.mileage-development-dialog\{[^}]*outline:0")
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

    def test_reimbursement_type_switcher_matches_approved_layout(self):
        markup = self.read_template("index.html")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('class="reimbursement-type-switcher"', markup)
        self.assertNotIn('class="reimbursement-breadcrumb"', markup)
        self.assertIn('id="travel-reimbursement-trigger"', markup)
        self.assertIn('id="expense-reimbursement-trigger"', markup)
        self.assertIn('role="tablist"', markup)
        self.assertIn('aria-selected="true"', markup)
        self.assertNotIn('<header class="page-header">', markup)
        self.assertNotIn('填写报销信息与行程明细，生成可下载的 Excel 和 PDF。', markup)
        self.assertIn("expenseTrigger.addEventListener('click'", script)
        self.assertIn("travelTrigger.addEventListener('click'", script)
        self.assertIn("reimbursement-type-switcher", styles)
        self.assertIn("background:#1d1d1f", styles)
        self.assertIn("min-height:52px", styles)
        self.assertIn("min-width:138px", styles)
        self.assertIn("font-size:16px", styles)
        self.assertIn("gap:12px", styles)
        self.assertIn("margin:0 0 28px", styles)
        self.assertIn("background:#fff", styles)
        self.assertIn("min-width:138px", styles)
        self.assertIn("min-height:52px", styles)
        self.assertIn("border-radius:26px", styles)
        self.assertIn("background:#fff", styles)
        self.assertRegex(styles, r"\.mileage-development-content p\{[^}]*font-weight:400")

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

    def test_history_tables_define_pagination_and_continuous_sequence_contract(self):
        markup = self.read_template("history.html")
        script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        components = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertIn('<th class="history-col-index">序号</th>', markup)
        self.assertIn('id="history-pagination"', markup)
        self.assertIn('id="history-previous-page"', markup)
        self.assertIn('id="history-next-page"', markup)
        self.assertIn('aria-label="上一页"', markup)
        self.assertIn('aria-label="下一页"', markup)
        self.assertIn('/static/icons.svg#chevron-left', markup)
        self.assertIn('/static/icons.svg#chevron-right', markup)
        self.assertIn('class="history-col-index"', markup)
        self.assertLess(markup.index('<th class="history-col-index">序号</th>'), markup.index('<th>出差事由</th>'))
        self.assertIn('const PAGE_SIZE = 15;', script)
        self.assertIn('const pageRecords = records.slice', script)
        self.assertIn('currentPage * PAGE_SIZE + index + 1', script)
        self.assertIn('previousPage.disabled = currentPage === 0', script)
        self.assertIn('nextPage.disabled = currentPage >= totalPages - 1', script)
        self.assertIn("pagination.hidden = records.length === 0", script)
        self.assertIn("previousPage.addEventListener('click'", script)
        self.assertIn("nextPage.addEventListener('click'", script)
        self.assertIn('.page-shell button.pagination-button', components)
        self.assertIn('width: 34px;', components)
        self.assertIn('min-height: 34px;', components)
        self.assertIn('.history-table tbody th.history-col-index', components)

    def test_history_background_fades_down_without_a_viewport_seam(self):
        markup = self.read_template("history.html")
        components = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")

        self.assertIn('<body class="app-body history-body"', markup)
        self.assertRegex(
            components,
            r"html body\.history-body\s*\{[^}]*min-height:\s*100vh",
        )
        self.assertRegex(
            components,
            r"html body\.history-body\s*\{[^}]*"
            r"background-image:\s*linear-gradient\(\s*180deg\s*,",
        )

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
        self.assertIn("xlsx.className = 'table-action-button secondary'", script)
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
        self.assertIn('body[data-scope="trash"] .history-table th:nth-child(6)', styles)
        self.assertIn('body[data-scope="trash"] .history-table td:nth-child(6)', styles)
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

    def test_active_history_actions_share_excel_button_width_without_global_change(self):
        styles = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertRegex(
            styles,
            r'body\[data-scope="active"\]\s+\.history-page\s+\.table-action-items\s+'
            r'\.table-action-button\s*\{[^}]*width:\s*96px[^}]*height:\s*34px[^}]*'
            r'min-height:\s*34px[^}]*justify-content:\s*center',
        )
        base_rule = styles.split('.table-action-button {', 1)[1].split('}', 1)[0]
        self.assertNotIn('width:', base_rule)

    def test_trash_history_actions_match_active_button_dimensions(self):
        styles = (ROOT / "static" / "ui-components.css").read_text(encoding="utf-8")
        self.assertRegex(
            styles,
            r'body\[data-scope="trash"\]\s+\.history-page\s+\.table-action-items\s+'
            r'\.table-action-button\s*\{[^}]*width:\s*96px[^}]*height:\s*34px[^}]*'
            r'min-height:\s*34px[^}]*justify-content:\s*center',
        )

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

    def test_index_loads_calendar_assets_and_preserves_date_contract(self):
        markup = self.read_template("index.html")
        self.assertIn('/static/calendar.css', markup)
        self.assertIn('/static/calendar.js', markup)
        self.assertEqual(markup.count('type="date"'), 12)
        self.assertIn('id="date" name="date" type="date"', markup)
        for row_number in range(1, 12):
            self.assertIn(
                f'name="row-{row_number}-date" type="date"',
                markup,
            )
        script = (ROOT / "static" / "calendar.js").read_text(encoding="utf-8")
        for token in ('input[type="date"]', 'aria-haspopup', 'aria-selected', 'formatToParts', 'date-picker-field-icon', 'calendar-days'):
            self.assertIn(token, script)
        for token in (
            'role',
            'date-picker-row',
            'date-picker-cell',
            'gridcell',
            'aria-expanded',
            'aria-controls',
            'dispatchEvent',
            'keydown',
            'Escape',
            'ArrowLeft',
            'ArrowRight',
            'ArrowUp',
            'ArrowDown',
            'Enter',
            'clearDate',
            'focusout',
            'scrollIntoView',
            'form.addEventListener(\'reset\'',
            'toIso',
            'padStart(2',
        ):
            self.assertIn(token, script)
        self.assertNotIn('new Date', script)
        self.assertNotIn('Date.UTC', script)
        self.assertIn('if (!today) return;', script)
        self.assertIn('return null;', script)
        icons = (ROOT / "static" / "icons.svg").read_text(encoding="utf-8")
        for icon in ('chevron-left', 'chevron-right', 'calendar-days'):
            self.assertIn(f'id="{icon}"', icons)

    def test_calendar_visual_contract(self):
        styles = (ROOT / "static" / "calendar.css").read_text(encoding="utf-8")
        for token in (
            '.date-picker-popover',
            'position: fixed',
            'grid-template-columns: repeat(7',
            'border-radius: 50%',
            'background: var(--ui-button-primary)',
            'color: #fff',
            '--ui-shadow-lg',
            'prefers-reduced-motion',
            'prefers-reduced-transparency',
        ):
            self.assertIn(token, styles)
        self.assertIn('.date-picker-field > input[data-calendar-enhanced]', styles)
        self.assertIn('.date-picker-clear', styles)

    def test_calendar_initialization_preserves_existing_detail_dates(self):
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        initialization = script.split("async function loadSession", 1)[0]
        self.assertNotIn("field.value = ''", initialization)
        self.assertIn("field.value = '';", script)

    def test_calendar_outside_click_restores_input_focus(self):
        script = (ROOT / "static" / "calendar.js").read_text(encoding="utf-8")
        outside_handler = script.split("function handleOutsidePointer", 1)[1].split(
            "function positionPopover", 1
        )[0]
        self.assertIn("closePopover(true);", outside_handler)

    def test_calendar_reset_restores_input_focus(self):
        script = (ROOT / "static" / "calendar.js").read_text(encoding="utf-8")
        reset_handler = script.split("form.addEventListener('reset'", 1)[1].split(
            "function enhanceInput", 1
        )[0]
        self.assertIn("closePopover(true);", reset_handler)

    def test_calendar_month_navigation_reuses_day_nodes(self):
        script = (ROOT / "static" / "calendar.js").read_text(encoding="utf-8")
        self.assertIn("const dayButtons = [];", script)
        self.assertIn("dayButtons[index]", script)
        self.assertNotIn("grid.textContent = '';", script)

    def test_calendar_focusout_does_not_close_on_internal_safari_navigation(self):
        script = (ROOT / "static" / "calendar.js").read_text(encoding="utf-8")
        self.assertIn("event.relatedTarget", script)
        self.assertIn("pointerInsidePopover", script)
        self.assertIn("popover.contains(nextTarget)", script)


if __name__ == "__main__":
    unittest.main()
