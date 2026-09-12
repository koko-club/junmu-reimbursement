# Version Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the project version to `v1.0.2` and display the current value from the shared `VERSION` source in every authenticated sidebar.

**Architecture:** Keep `VERSION` as the single source of truth. Add a small server-side version reader in `web.py`, route all authenticated HTML pages through the existing template replacement path, and replace a dedicated `__APP_VERSION__` marker in the three sidebar templates with the escaped value read from disk.

**Tech Stack:** Python 3, standard-library HTTP server, plain HTML templates, unittest.

---

### Task 1: Add regression coverage for version sourcing and sidebar markup

**Files:**
- Modify: `tests/test_frontend.py`
- Modify: `tests/test_web_auth.py`
- Modify: `tests/http_helpers.py`

- [ ] **Step 1: Add static template contract assertions**

In `FrontendContractTest`, add a test that reads `index.html`, `history.html`, and `admin.html`, then asserts each contains `class="sidebar-version"`, the `版本：` label, and the `__APP_VERSION__` marker. This verifies the marker is present without coupling the test to a hard-coded release value.

- [ ] **Step 2: Add an HTTP rendering regression test**

In `WebAuthenticationTest`, after setup and login, request `/`, `/history`, and `/admin`; assert status `200`, assert each response contains `版本：v1.0.1` based on the repository fixture's current version before the version bump, and assert `__APP_VERSION__` is absent. The implementation step will then update the expected value to `v1.0.2` together with `VERSION`.

- [ ] **Step 3: Run the focused tests and confirm the new behavior fails**

Run:

```bash
python -m unittest tests.test_frontend.FrontendContractTest tests.test_web_auth.WebAuthenticationTest -v
```

Expected: the new sidebar marker/rendering assertions fail because the templates and server do not yet inject a version.

### Task 2: Implement the single-source version injection

**Files:**
- Modify: `VERSION`
- Modify: `web.py:522-555,1044-1065`
- Modify: `templates/index.html`
- Modify: `templates/history.html`
- Modify: `templates/admin.html`

- [ ] **Step 1: Bump the release source**

Replace the complete contents of `VERSION` with the single line `v1.0.2` and a trailing newline.

- [ ] **Step 2: Add a server-side version reader**

Add a module-level `VERSION_PATH = Path(__file__).resolve().parent / "VERSION"` and a helper:

```python
def _load_app_version() -> str:
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("application version unavailable") from error
    if not version:
        raise RuntimeError("application version unavailable")
    return version
```

Update `_root` to call `_template_page(handler, "index.html", "")` so the homepage uses the same replacement pipeline. Update `_template_page` to load the version once, replace `__APP_VERSION__` with `html.escape(version)`, and preserve the existing CSRF and history-scope replacements. If the version cannot be loaded, return the same `500` JSON error used for unavailable templates.

- [ ] **Step 3: Add the version element to every authenticated sidebar**

Immediately after each existing `sidebar-account` block in `templates/index.html`, `templates/history.html`, and `templates/admin.html`, add:

```html
<div class="sidebar-version">版本：<span id="app-version">__APP_VERSION__</span></div>
```

- [ ] **Step 4: Update the HTTP regression expectation to `v1.0.2`**

Change the test introduced in Task 1 to assert the new release value, while continuing to assert the placeholder is absent.

- [ ] **Step 5: Run the focused tests and confirm they pass**

Run:

```bash
python -m unittest tests.test_frontend.FrontendContractTest tests.test_web_auth.WebAuthenticationTest -v
```

Expected: PASS, including version injection for `/`, `/history`, and `/admin`.

### Task 3: Verify integration and documentation consistency

**Files:**
- Modify: `README_部署说明.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update release references**

Change the current-version statement in `README_部署说明.md` to `v1.0.2`. Add a `v1.0.2` entry at the top of `CHANGELOG.md` describing the sidebar version display and single-source version update, following the file's existing format.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
python -m unittest discover -s tests -v
```

Expected: all tests pass.

- [ ] **Step 3: Inspect the final diff for scope and placeholder leaks**

Run:

```bash
git diff -- VERSION web.py templates/index.html templates/history.html templates/admin.html tests/test_frontend.py tests/test_web_auth.py README_部署说明.md CHANGELOG.md
rg -n "__APP_VERSION__|版本：v1\.0\.1|当前版本：`v1\.0\.1`" VERSION web.py templates tests README_部署说明.md CHANGELOG.md
```

Expected: only template source files retain `__APP_VERSION__`; rendered tests contain `v1.0.2`; no current-version documentation still points to `v1.0.1`.

- [ ] **Step 4: Commit the scoped implementation**

```bash
git add VERSION web.py templates/index.html templates/history.html templates/admin.html tests/test_frontend.py tests/test_web_auth.py README_部署说明.md CHANGELOG.md docs/superpowers/specs/2026-09-12-version-display-design.md docs/superpowers/plans/2026-09-12-version-display.md
git commit -m "feat: display current application version in sidebar"
```

