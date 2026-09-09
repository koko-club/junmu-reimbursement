# Apple UI Design Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update every reimbursement-system page to a restrained Apple white system-settings visual language without changing business behavior.

**Architecture:** Keep all existing routes, form controls, IDs, data attributes, JavaScript, and Python untouched. Centralize the visual system in `static/styles.css`, making only minimal template class/semantic additions where needed for consistent navigation and page chrome.

**Tech Stack:** Server-rendered HTML, CSS, vanilla JavaScript, existing Python web server and pytest suite.

---

### Task 1: Establish shared Apple visual tokens and responsive chrome

**Files:**
- Modify: `static/styles.css`

- [ ] Replace legacy palette with low-saturation gray-blue action tokens, Apple system font stack, warm light-gray canvas, white surfaces, 8-12px radii, and restrained shadows.
- [ ] Add explicit `:active` press feedback, focus-visible rings, reduced-motion and reduced-transparency media rules.
- [ ] Restyle `.app-sidebar`, `.page-shell`, `.page-header`, buttons, inputs, results, dialogs, and responsive sidebar states while preserving selectors used by existing JS.
- [ ] Keep table density and horizontal scrolling behavior intact; style headers and rows without changing markup contracts.

### Task 2: Align page templates with shared hierarchy

**Files:**
- Modify: `templates/index.html`
- Modify: `templates/history.html`
- Modify: `templates/trash.html` if present
- Modify: `templates/admin.html`
- Modify: `templates/login.html`
- Modify: `templates/register.html`
- Modify: `templates/change-password.html`
- Modify: `templates/setup.html`

- [ ] Preserve every existing form field, `id`, `name`, `data-*` attribute, script include, route, and accessibility label.
- [ ] Add only shared visual hooks needed for page title metadata, navigation grouping, and status text; do not alter submission structure.
- [ ] Ensure all pages use the same navigation brand, active state, content header, panel spacing, and action hierarchy.

### Task 3: Verify behavior and visual regressions

**Files:**
- Test: `tests/test_frontend.py`

- [ ] Run focused frontend tests: `pytest tests/test_frontend.py -q`.
- [ ] Run full suite: `pytest -q`.
- [ ] Start the local server and inspect desktop and narrow viewport rendering for login, form, history, trash, and admin pages.
- [ ] Confirm existing JS selectors resolve and no console errors occur; confirm reduced-motion CSS is present.
