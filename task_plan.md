# Task Plan: 在线报销系统用户与历史功能

## Goal
在现有差旅报销网页工具基础上增加管理员、普通用户登录、普通用户单会话限制，以及按用户隔离的 Excel/PDF 生成历史。

## Current Phase
Task 6 canonical login-limiter identity follow-up complete and verified; Task 7 has not started.

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
