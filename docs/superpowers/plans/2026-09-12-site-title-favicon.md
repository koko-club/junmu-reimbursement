# Unified Site Title and Favicon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every rendered reimbursement-system page use the browser title “在线报销系统” and the user-provided image at `/static/favicon.png` as its favicon, while preserving the existing sidebar brand.

**Architecture:** Keep page metadata explicit in each server-rendered template, remove the recycle-bin script's alternate document title, and make the low-level fallback HTML generator emit the same site title and favicon link. Store the supplied PNG as a separate static asset so `static/logo.png` remains untouched.

**Tech Stack:** Python `http.server` application, server-rendered HTML templates, vanilla JavaScript, PNG static assets, Python `unittest` contract tests.

---

### Task 1: Add failing metadata contract tests

**Files:**
- Modify: `tests/test_frontend.py` after `test_templates_load_shared_assets_and_bound_csrf_marker`
- Modify: `tests/test_web_auth.py` after the existing page/authentication tests
- Test: `templates/setup.html`, `templates/register.html`, `templates/login.html`, `templates/change-password.html`, `templates/index.html`, `templates/history.html`, `templates/admin.html`, `static/history.js`

- [ ] **Step 1: Write the failing test**

Add this method to `FrontendContractTest`:

```python
    def test_all_pages_use_site_title_and_favicon(self):
        pages = (
            "setup.html",
            "register.html",
            "login.html",
            "change-password.html",
            "index.html",
            "history.html",
            "admin.html",
        )
        for page in pages:
            with self.subTest(page=page):
                markup = self.read_template(page)
                self.assertRegex(markup, r"<title(?:\\s[^>]*)?>在线报销系统</title>")
                self.assertIn(
                    '<link rel="icon" type="image/png" href="/static/favicon.png">',
                    markup,
                )
        favicon = ROOT / "static" / "favicon.png"
        self.assertTrue(favicon.is_file())
        self.assertEqual(favicon.read_bytes()[:8], b"\\x89PNG\\r\\n\\x1a\\n")
        history_script = (ROOT / "static" / "history.js").read_text(encoding="utf-8")
        self.assertNotIn("document.title = '回收站'", history_script)
```

Add this HTTP-level test to `WebAuthenticationTest`:

```python
    def test_rendered_pages_expose_site_metadata_and_favicon(self):
        title = "<title>在线报销系统</title>"
        favicon_link = '<link rel="icon" type="image/png" href="/static/favicon.png">'

        def assert_page(response):
            self.assertEqual(response.status, 200)
            self.assertIn(title, response.text)
            self.assertIn(favicon_link, response.text)

        assert_page(self.client.get("/setup"))
        favicon = self.client.get("/static/favicon.png")
        self.assertEqual(favicon.status, 200)
        self.assertEqual(favicon.headers["Content-Type"].split(";", 1)[0], "image/png")
        self.assertTrue(favicon.body.startswith(b"\\x89PNG\\r\\n\\x1a\\n"))

        self.setup_admin()
        assert_page(self.client.get("/login"))
        assert_page(self.client.get("/register"))
        self.register()
        self.approve_user()
        self.client.post_json("/api/login", {"username": "admin", "password": ADMIN_PASSWORD})
        assert_page(self.client.get("/admin"))
        assert_page(self.client.get("/change-password"))

        user_client = self.running.new_client()
        user_client.post_json("/api/login", {"username": "alice", "password": USER_PASSWORD})
        for path in ("/", "/history", "/trash"):
            with self.subTest(path=path):
                assert_page(user_client.get(path))
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_all_pages_use_site_title_and_favicon
```

Expected: FAIL because the existing templates use page-specific titles, no favicon asset/link exists, and `history.js` assigns “回收站”.

Run the HTTP test as well:

```bash
python3 -m unittest tests.test_web_auth.WebAuthenticationTest.test_rendered_pages_expose_site_metadata_and_favicon
```

Expected: FAIL because `/static/favicon.png` does not exist and the rendered templates do not contain the requested metadata.

### Task 2: Add the supplied favicon and update rendered page metadata

**Files:**
- Create: `static/favicon.png` (copy of `/var/folders/1_/07yz_m8x07n9l2b45vyv61pr0000gn/T/codex-clipboard-6783cc57-0c55-4cc0-93d5-f236cea86cb2.png`)
- Modify: `templates/setup.html`
- Modify: `templates/register.html`
- Modify: `templates/login.html`
- Modify: `templates/change-password.html`
- Modify: `templates/index.html`
- Modify: `templates/history.html`
- Modify: `templates/admin.html`
- Modify: `static/history.js:19`
- Modify: `tests/http_helpers.py` in the `RunningApp.__enter__` static-file copy list

- [ ] **Step 1: Copy the user-provided PNG without changing `static/logo.png`**

Run:

```bash
cp /var/folders/1_/07yz_m8x07n9l2b45vyv61pr0000gn/T/codex-clipboard-6783cc57-0c55-4cc0-93d5-f236cea86cb2.png static/favicon.png
```

Update the `RunningApp.__enter__` copy list in `tests/http_helpers.py` so integration tests receive the new asset:

```python
            for static_name in ("common.js", "history.js", "admin.js", "styles.css", "favicon.png"):
```

Keep the existing conditional `if source.is_file()` and all other copied filenames unchanged.

- [ ] **Step 2: Update each template head**

Replace each page-specific title with the exact line below and add the favicon link immediately after it. Preserve the existing `data-page-title` attribute in `history.html`:

```html
<title>在线报销系统</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
```

For `history.html`, use:

```html
<title data-page-title>在线报销系统</title>
<link rel="icon" type="image/png" href="/static/favicon.png">
```

- [ ] **Step 3: Stop the recycle-bin view from changing the browser title**

In `static/history.js`, keep the heading label/icon changes inside the `scope === 'trash'` block but delete only this statement:

```javascript
document.title = '回收站';
```

- [ ] **Step 4: Run the focused frontend test**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_all_pages_use_site_title_and_favicon
```

Expected: PASS.

- [ ] **Step 5: Run the HTTP metadata test**

Run:

```bash
python3 -m unittest tests.test_web_auth.WebAuthenticationTest.test_rendered_pages_expose_site_metadata_and_favicon
```

Expected: PASS with HTTP 200 HTML responses for `/setup`, `/login`, `/register`, `/admin`, `/change-password`, `/`, `/history`, and `/trash`, plus an HTTP 200 `image/png` response for `/static/favicon.png`.

### Task 3: Keep fallback HTML metadata consistent

**Files:**
- Modify: `web.py` near `VERSION_PATH` and `WebApplication._page_markup`
- Test: `tests/test_frontend.py`

- [ ] **Step 1: Extend the failing contract for fallback markup**

Add this method to `FrontendContractTest`:

```python
    def test_fallback_page_markup_uses_site_title_and_favicon(self):
        from web import WebApplication

        markup = WebApplication._page_markup("内部错误").decode("utf-8")
        self.assertIn("<title>在线报销系统</title>", markup)
        self.assertIn(
            '<link rel="icon" type="image/png" href="/static/favicon.png">',
            markup,
        )
        self.assertIn("<h1>内部错误</h1>", markup)
```

- [ ] **Step 2: Run the new test to verify it fails**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_fallback_page_markup_uses_site_title_and_favicon
```

Expected: FAIL because `_page_markup` currently uses its `title` argument for the `<title>` and emits no favicon link.

- [ ] **Step 3: Add a site-name constant and update only the fallback `<head>`**

Near `VERSION_PATH`, add:

```python
SITE_NAME = "在线报销系统"
```

Change `_page_markup` so its generated markup retains the caller-provided heading but uses the constant and favicon for browser metadata:

```python
    @staticmethod
    def _page_markup(title: str, csrf_token: str | None = None) -> bytes:
        csrf_attribute = "" if csrf_token is None else f' data-csrf="{html.escape(csrf_token)}"'
        markup = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(SITE_NAME)}</title>"
            '<link rel="icon" type="image/png" href="/static/favicon.png"></head>'
            f"<body{csrf_attribute}><main><h1>{html.escape(title)}</h1></main></body></html>"
        )
        return markup.encode("utf-8")
```

- [ ] **Step 4: Run the fallback test**

Run:

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest.test_fallback_page_markup_uses_site_title_and_favicon
```

Expected: PASS.

### Task 4: Run regression and source verification

**Files:**
- Verify: `templates/*.html`, `static/favicon.png`, `static/history.js`, `web.py`

- [ ] **Step 1: Run all frontend contract tests**

Run:

```bash
python3 -m unittest tests.test_frontend
```

Expected: PASS with no failures.

- [ ] **Step 2: Run the server/auth/static regression tests**

Run:

```bash
python3 -m unittest tests.test_server tests.test_web_auth tests.test_docker_assets
```

Expected: PASS. Existing uncommitted tests or unrelated worktree changes must remain untouched.

- [ ] **Step 3: Verify static bytes and source scope**

Run:

```bash
file static/favicon.png
git diff --check
rg -n "<title|rel=\"icon\"|document\.title" templates static/history.js web.py
```

Expected: `static/favicon.png` is reported as PNG; each template has exactly one site title/favicon pair; `history.js` has no assignment to “回收站”; the existing `/static/logo.png` references remain unchanged.

- [ ] **Step 4: Commit the implementation**

```bash
git add templates static/history.js static/favicon.png web.py tests/test_frontend.py tests/test_web_auth.py tests/http_helpers.py
git commit -m "feat: unify site title and favicon"
```
