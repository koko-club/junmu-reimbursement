# Personal Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a personal center for ordinary users with self-service name and department editing and a monthly travel-days curve.

**Architecture:** Reuse `/api/session` for profile reads; add a CSRF-protected self-update route and an owner-scoped monthly aggregation route. Store no new columns: read the existing normalized reimbursement payload, with the existing Excel fallback for old records. Render the 12-month line in a new page with native SVG and a numeric text summary.

**Tech Stack:** Python standard-library HTTP server, SQLite, unittest, vanilla JavaScript, SVG, CSS.

---

### Task 1: Self profile update

**Files:** `tests/test_users.py`, `tests/test_web_auth.py`, `users.py`, `web.py`

- [ ] Add a `UserService.update_own_profile(user_id, real_name, department)` test: it changes only the identified active user and rejects invalid values. Run `python3 -m unittest tests.test_users -v` and confirm the new test fails because the method is missing.
- [ ] Implement the method using `_profile_value`, `BEGIN IMMEDIATE`, and an active-user check; do not modify username or existing reimbursements. Run the same test to green.
- [ ] Add HTTP tests for `POST /api/profile` covering successful self-edit, session reflection, username-field rejection, missing CSRF, and unauthenticated request. Confirm the tests fail before adding the route.
- [ ] Add the protected route and response. Use the existing `_json_body`, `_required_strings`, `UserError`, and route CSRF conventions. Run `python3 -m unittest tests.test_web_auth -v` to green.

### Task 2: Monthly travel-day aggregation

**Files:** `tests/test_reimbursements.py`, `tests/test_web_reimbursements.py`, `reimbursements.py`, `web.py`

- [ ] Write a service test with a six-day trip having two distinct September dates and one October date; expect 4 and 2 days. Add cases for duplicate dates, missing/invalid dates, zero days, an expense record, a trashed record, and a different owner. Run `python3 -m unittest tests.test_reimbursements -v` and confirm failure because the aggregation method is missing.
- [ ] Implement `travel_days_by_month(user_id)` against active owner records. Parse travel `payload_json`; for old records call existing `edit_details` for the Excel fallback. Deduplicate valid ISO dates within a record, group their months, divide the record's `days` by distinct-date count, and return ordered monthly totals plus an excluded count. Preserve record total by assigning rounding residue to the final month. Run the service tests to green.
- [ ] Add an HTTP test for authenticated `GET /api/travel-days` and owner isolation. Confirm red, add route/handler, then run `python3 -m unittest tests.test_web_reimbursements -v` to green.

### Task 3: Page and curve

**Files:** `tests/test_frontend.py`, `templates/index.html`, `templates/history.html`, `templates/profile.html`, `static/profile.js`, `static/profile.css`, `web.py`, `static/icons.svg`

- [ ] Add frontend contract tests for the new page, all ordinary-user navigation links, read-only username, editable name/department, password link, chart controls and accessible summary. Confirm red with `python3 -m unittest tests.test_frontend -v`.
- [ ] Add `GET /profile`, serve the template, and insert the sidebar link after 回收站 in both ordinary-user templates. Create an icon using the existing symbol set or add one symbol if needed.
- [ ] Build the page: fetch `/api/session` and `/api/travel-days`; submit profile with `apiFetch('/api/profile', {method:'POST', ...})`; populate available years from data; render a 12-point SVG line for the chosen year, with a zero baseline, readable axes, point labels, and a text list of months; choosing a month highlights its point and updates the callout. Handle empty and failed loads with visible messages and a retry button. Reuse existing visual tokens and narrow-screen rules.
- [ ] Run `python3 -m unittest tests.test_frontend -v`; inspect the page in a browser or render and check narrow-screen layout.

### Task 4: Final verification

- [ ] Run `python3 -m unittest discover -s tests -v` and resolve feature-related failures.
- [ ] Run `node --check static/profile.js`, `python3 -m compileall -q users.py reimbursements.py web.py`, and `git diff --check`.
- [ ] Review the diff against the design specification and report tests, behavior, and remaining limits.
