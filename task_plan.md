# Task Plan: 在线报销系统用户与历史功能

## Goal
在现有差旅报销网页工具基础上增加管理员、普通用户登录、普通用户单会话限制，以及按用户隔离的 Excel/PDF 生成历史。

## Current Phase
Tasks 1-10 are complete after specification and independent quality approval. Task 13 backup/deployment assets are complete but container verification remains environment-blocked. Tasks 11-12 and 14 remain pending. User confirmed continuous execution.

## Task 8 Review Follow-up
- [x] Implement private history, descriptor-streamed downloads, recycle bin, and daily purge
- [x] Add durable authenticated pending/completion markers, retryable purge claims, bounded upload admission and shutdown
- [x] Fix crash recovery, replaced-directory handling, UUID reuse, and committed download response errors
- [x] Keep final purge validation/deletion on a private database connection
- [x] Independent verification at `d621b40`: 182 focused and 338 complete tests passed
- [x] Final specification review approved `d621b40`
- [x] Preserve successful results across post-commit database close/commit and upload cleanup errors
- [x] Specification regression and independent quality approval
- [x] Mark Task 8 complete and continue Tasks 9-14

## Task 7 Review Follow-up
- [x] RED: prove an exporter cannot replace, delete, or change the workbook into a non-regular file after its first validation
- [x] GREEN: revalidate workbook and PDF outputs, preserve workbook identity, and check distinctness inside the semaphore immediately before deriving move paths
- [x] RED: prove unsafe or non-UTF-8 display names cannot reach logs, storage, or history
- [x] GREEN: validate a legal `.xlsx` basename against a documented UTF-8 byte limit before persistence
- [x] Run focused, real LibreOffice end-to-end, full-suite, compile, and diff checks
- [x] Self-review the scoped follow-up without FIFO queueing or marking Task 7 complete

## Task 7 Formal Quality Review Follow-up
- [x] RED: reproduce cleanup TOCTOU after visible `tmp`, `users`, and owner path replacement without losing external sentinels
- [x] GREEN: anchor generation, move, and recursive cleanup to verified directory file descriptors and close every descriptor
- [x] RED: reproduce generator hardlinks, validation-to-move inode swaps, owned-file hardlinks, and post-validation read races
- [x] GREEN: bind generated outputs and owner downloads to `O_NOFOLLOW` file descriptors with single-link and inode checks
- [x] RED: reproduce rejection of valid 50/100-character Chinese profile names after expensive generation
- [x] GREEN: compute a bounded safe output stem before generation while preserving complete workbook profile values
- [x] RED/GREEN: isolate every cleanup candidate before final unlink/rmdir and reject artifact FIFOs without blocking
- [x] Update the Task 7 owned-file plan contract, run all required verification, review, and commit; keep Task 7 in progress

## Task 7 Request-directory Creation Follow-up
- [x] RED: inject one-shot open and post-open identity failures for work and final claim directories
- [x] RED: prove shared tmp/users/owner directories survive common-directory open failures
- [x] GREEN: create, open, and capture identity for request-owned directories in one rollback-safe helper
- [x] Run focused/full ResourceWarning, real LibreOffice E2E, compile, diff, review, and commit; keep Task 7 in progress

## Task 7 Initial-statat Follow-up
- [x] RED: inject one-shot initial statat failures for work and final claim UUID directories
- [x] GREEN: bind ownership to the first no-follow stat, require the opened fd to match, and retain UUID entries when that first identity check fails
- [x] Preserve collision, persistent-inspection-failure, shared-directory, and fd-close behavior
- [x] Run new/focused/full ResourceWarning, real LibreOffice E2E, compile, diff, review, and commit follow-up; keep Task 7 in progress

## Design Checklist
- [x] Explore current project location and existing architecture
- [x] Confirm whether to preserve the old version and build in this directory
- [x] Clarify registration fields, approval behavior, and account recovery
- [x] Propose 2-3 implementation approaches
- [x] Present architecture, data model, flows, errors, and tests for approval
- [x] Write and self-review design specification
- [x] Obtain user review of written specification
- [x] Write implementation plan

## Constraints
- Preserve existing Excel/PDF generation behavior and template formatting.
- Ordinary users self-register and require administrator approval before login.
- Ordinary users cannot have multiple simultaneous login sessions.
- Each ordinary user can see and download only their own generated history.
- All generation, history, and download authorization must be enforced server-side; hiding links in the browser is insufficient.
- Develop on the current Mac and deploy to Synology DS925+ (`linux/amd64`) with Docker.
- Serve the LAN-only deployment on port 8800 by default.

## Errors Encountered
| Error | Attempt | Resolution |
|---|---:|---|
| Current workspace is empty | 1 | Treat existing project under `其他/reimbursement_form_app_20260903` as source after user confirms copy strategy |
| Visual companion could not bind inside the sandbox | 1 | Started the approved localhost-only design server outside the sandbox |
| Confirmed-HEAD test replacement had an indentation error | 1 | Corrected the assertion block and reran focused, HTTP, and full verification before commit |
| Repeated deadline stress produced one delayed header response and one unframed body response | 1 | Replaced timer plus `SHUT_RD` with a synchronous deadline-aware raw reader; the same 9-test stress sequence passes |
| Canonical limiter follow-up planning patch used mistyped context | 1 | Re-read the planning files and applied the update against the exact current text |
| Focused RED command selected system Python 3.9 and failed on supported union syntax before collecting tests | 1 | Re-ran with the Codex bundled Python runtime used by the project verification suite |
| HTTP/auth verification referenced absent planned module `tests.test_request_security` | 1 | Derived the actual verification groups from the current test tree and prior 55-test total: `test_web_auth` plus `test_server` |
| Sandboxed HTTP test could not bind its temporary localhost port | 1 | Re-ran the same focused test with approved localhost binding and captured the behavioral RED result |
| Verbose full-suite output did not retain its final summary in the tool capture | 1 | Re-ran the complete suite quietly and captured the definitive 180-test count and zero exit status |
| Combined surrogate-test patch mismatched escaped Unicode context in `test_users.py` | 1 | Confirmed the patch was atomic with no test changes, then split it into smaller insertions anchored on ASCII-only lines |
| Task 7 nested-path implementation patch mixed a `progress.md` context line into the `reimbursements.py` block | 1 | Confirmed the patch was atomic, then split implementation and progress updates into exact file-specific patches |
| Sandboxed Task 7 full suite could not bind temporary localhost ports | 1 | Re-ran the identical 217-test suite outside the sandbox with approved localhost binding and captured a definitive quiet summary |
| Sandboxed `git add` could not create `.git/index.lock` | 1 | Keep the working tree unchanged and rerun the exact staging command outside the sandbox with approval |
| First follow-up full-suite handoff omitted the final count and exit status | 1 | Re-ran in a persistent terminal session and captured 226 tests, `OK`, and exit status 0 |
| Follow-up syntax command referenced nonexistent `auth.py` | 1 | Enumerated the repository modules and reran `py_compile` against the actual root Python files |
| Formal follow-up plan patch used the wrong Task 8 test filename context | 1 | Confirmed the patch was atomic, re-read the exact plan section, and applied smaller file-specific changes |
| Long-name service test omitted `load_workbook` import | 1 | Added the test dependency and reran the same three-test RED/GREEN set |
| Long-name E2E retained the old `张三` PDF assertion | 1 | Changed the assertion to the complete 100-character traveler and reran the real LibreOffice test |
| Generated-FIFO test patch used an outdated adjacent assertion | 1 | Re-read the current test section and reapplied with the exact local context |
| Sandboxed full suite could not bind loopback sockets (`PermissionError`) | 1 | Re-run the same 244-test command with the required local-network sandbox permission |
| Shared-directory characterization reused the no-files helper despite precreated sentinels | 1 | Replaced only its final assertion with zero database rows and zero XLSX/PDF artifacts |
| Identity-failure tests counted only creation fds, but rollback safely reopened the same UUID | 1 | Assert every captured creation and rollback fd is closed instead of assuming the pre-fix fd count |
| First complete-suite capture omitted its final unittest summary and exit status after expected fault-injection logs | 1 | Re-ran through a persistent buffered unittest session and captured 255 tests, `OK`, and exit status 0 |
| First Initial-statat review fix moved unsafe identity inference from failed open recovery to failed stat recovery | 1 | Added the symmetric replacement regression and retained any UUID entry whose first open cannot establish a bound identity |
| Three mkdir/open/stat orderings each allowed a replacement inode to become the rollback identity | 3 | Stop rearranging non-atomic operations; require an explicit contract decision between conservative retention and guaranteed transient-failure cleanup |
