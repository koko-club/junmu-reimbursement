# Reimbursement Statistics Implementation Plan

> **For agentic workers:** Execute task-by-task with tests first. Steps use checkbox syntax for tracking.

**Goal:** Show current-user monthly count, yearly count, and yearly amount on the reimbursement form using active history records grouped by generation date.

**Architecture:** Add a protected `GET /api/reimbursements/stats` route backed by `ReimbursementService.active_stats`. The service filters `deleted_at IS NULL`, converts UTC `created_at` values to `Asia/Shanghai`, and aggregates counts plus exact Decimal amount. The form renders three Aurora Glass cards and refreshes on load, visibility regain, and the `storage` notification emitted by history actions.

**Tech Stack:** Python standard library (`datetime`, `zoneinfo`, `Decimal`), SQLite, vanilla JavaScript, existing Aurora Glass CSS, `unittest`.

## File Map

- `reimbursements.py`: active-history statistics value object and aggregation method.
- `web.py`: authenticated stats route and JSON serialization.
- `templates/index.html`: three cards above the basic-information section.
- `static/app.js`: stats loading, formatting, and refresh listeners.
- `static/history.js`: notify other tabs after active-record mutations.
- `static/styles.css`: responsive Aurora Glass stat-card presentation.
- `tests/test_reimbursements.py`: aggregation and recycle-bin exclusion tests.
- `tests/test_web_reimbursements.py`: authentication and stats payload contract.
- `tests/test_frontend.py`: markup, route, formatting, and refresh contracts.

## Tasks

### Task 1: Add failing contracts

- [x] Add service, HTTP, and frontend tests for generated-date grouping, active-only filtering, `¥` formatting, and refresh notifications.
- [x] Run the focused tests and verify they fail for the missing stats behavior.

### Task 2: Implement the protected statistics API

- [x] Add `active_stats(user_id, now=None)` using UTC-to-Asia/Shanghai conversion and Decimal-safe amount summation.
- [x] Register `/api/reimbursements/stats` for authenticated users and return `{month_count, year_count, year_amount}`.
- [x] Run service and HTTP tests until green.

### Task 3: Render and refresh the form cards

- [x] Add three cards before `#basic-heading`, with stable IDs and accessible labels.
- [x] Load stats alongside the session profile, render counts with `笔`, and render amount with `¥` plus two decimals.
- [x] Refresh on `visibilitychange` and the cross-tab `storage` event; keep form usable if stats fail.
- [x] Add Aurora Glass hover/focus-safe responsive styling.

### Task 4: Notify from history and verify

- [x] Emit a same-origin local-storage update after trash, restore, or purge succeeds.
- [x] Run all frontend tests, relevant reimbursement/web tests with the bundled Python runtime, JavaScript syntax checks, and `git diff --check`.
- [x] Inspect desktop and narrow screenshots for clipping and overlap.
