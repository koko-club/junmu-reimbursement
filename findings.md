# Findings

## Project Context
- `/Users/koko/Documents/Codex/在线报销系统` is empty at the start of this feature request.
- The current working reimbursement app is `/Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903`.
- The current app is a standard-library Python `ThreadingHTTPServer` with HTML/JS frontend, Excel generation through `openpyxl`/Pillow, and PDF conversion through LibreOffice.
- Generated files currently share one `generated/` directory and downloads are not authenticated.
- The existing portable deployment package targets macOS arm64 and also supports source-only deployment.

## Requested Behavior
- Users must log in.
- Ordinary users self-register from an “立即注册” entry and cannot log in until an administrator approves the application.
- An ordinary user must not be logged in from multiple places at once.
- After login, an ordinary user sees their own historical Excel/PDF outputs.

## Open Decisions
- None for the approved product design. Implementation details must remain within the written specification.

## Confirmed Decisions
- Build the upgraded system in `/Users/koko/Documents/Codex/在线报销系统` by copying the current version later; keep the existing working project untouched as a rollback copy.
- Bootstrap the first administrator through a one-time browser setup page. The setup route is available only while no administrator exists and permanently closes after successful creation.
- Ordinary-user single-session policy: a successful login on a new device revokes the previous session immediately. The old device is redirected to login on its next authenticated request.
- Administrators manage accounts only. They cannot list or download ordinary users' reimbursement history; history is private to the owning ordinary user.
- Existing shared `generated/` files are not migrated because they have no reliable owner. Only files generated after authentication is enabled appear in per-user history; the original project and files remain untouched.
- User replaced the original “administrator creates ordinary users” requirement with self-registration plus administrator approval. The administrator can approve or reject pending applications.
- Registration requires username, password, real name, and department. History ownership is always the authenticated account ID, never a traveler-name match.
- For ordinary users, traveler and department are prefilled from the approved profile and locked in the reimbursement form.
- Users may change their own passwords. Administrators can reset a forgotten password to a temporary value but cannot view the existing password; the user must change it at next login. Password changes/resets revoke current sessions.
- Rejected registration applications are deleted and the username is released; applicants must register again.
- Login sessions persist for up to 7 days unless manually logged out, revoked by password/account changes, or replaced by a new ordinary-user login.
- Administrators also use strict single-session login; a new administrator login revokes the old session.
- Administrators can disable/enable approved ordinary users and reset passwords. Approved users and their historical files cannot be deleted through the admin UI. Disabling immediately revokes the user's session.
- Ordinary users can delete their own individual history records and corresponding Excel/PDF files. Ownership is checked server-side before deletion.
- User-deleted history moves to a recycle bin for 30 days. The owner can restore it or permanently delete it immediately; expired records and files are purged automatically.
- Only administrators can edit an approved user's real name or department. Changes apply to new forms; existing generated files remain unchanged.
- Development and debugging run on the current Mac. Final delivery is a Docker deployment for Synology NAS, with durable database/generated-file storage in mounted volumes and LibreOffice inside the container.
- The target NAS is a Synology DS925+ with AMD Ryzen V1500B (`x86_64`); the Docker image target is `linux/amd64`.
- Deployment is LAN-only and uses HTTP on port 8800 by default. HTTPS remains an optional future reverse-proxy configuration.
- User chose the existing-stack approach: retain `ThreadingHTTPServer`, add SQLite and focused authentication/storage modules, and avoid a framework migration.
- User approved the architecture, data model, authentication flow, per-user generation/history authorization, recycle-bin behavior, Docker persistence, errors, and test strategy.
- User selected the left-sidebar interface layout in the visual companion. On narrow screens the sidebar collapses to a menu button.
- The approved design is documented at `docs/superpowers/specs/2026-09-07-auth-user-history-design.md`.

## Security Findings
- Current routes `/`, `/generate`, and `/download/<filename>` have no authentication.
- Download authorization is currently based only on the filename being inside the shared output directory; adding a login page alone would not isolate users.
- Per-user history requires durable ownership metadata and server-side checks on both history listing and file download.
- `ThreadingHTTPServer` already supports simultaneous requests, but authentication/session/database operations must be concurrency-safe.
- Task 6 specification re-review found that callback-level connection timeouts can be converted into a second JSON 500 inside `_dispatch`, even though the outer `_safely` path already treats disconnects as terminal.
- The pre-handler saturation response is constructed directly in `BoundedThreadingHTTPServer`, so it must apply HEAD body suppression itself rather than relying on `WebApplication._send()`.
- At `1fea5d7`, saturation treated an empty nonblocking read as a possible HEAD request because `b"HEAD ".startswith(b"")` is true. The corrected rule suppresses the body only after observing a complete `HEAD ` token; empty, partial, and unknown prefixes retain complete JSON framing without delaying admission.
- At `db0ac59`, the request socket timeout was an idle timeout reused by `BaseHTTPRequestHandler` and body reads. Repeated bytes arriving just inside that interval could hold a handler slot indefinitely; request-line plus headers now use one monotonic phase deadline, and body reading gets a fresh monotonic deadline that ends before the business callback runs.
- At `db0ac59`, login throttling reserved only `(normalized_username, client_ip)` in an unbounded dictionary. Rotating usernames bypassed the per-key budget, and attacker-controlled usernames could become large retained keys. Login now uses bounded canonical account keys plus independent client-IP and fixed global budgets, backed by a limiter with a hard key cap and fail-closed saturation.
- The chosen login policy preserves the approved 5-attempt account+IP budget and adds 10 attempts per client IP plus 100 attempts globally per 15-minute window. Limiter storage is capped at 4096 keys; capacity exhaustion without expired entries fails closed, and successful login clears only the account+IP key.
- A timer that interrupts reads with `SHUT_RD` is not reliable for framed timeout responses while the peer continues sending: stress testing observed a discarded response and a delayed close. A raw reader that recalculates the remaining monotonic budget for every `recv_into` enforces the same absolute deadline without background timer or socket-shutdown races.
- Task 6 already owns setup/register/login throttling and the shared scrypt concurrency budget; Task 9 must retain only its remaining admin API, request-ID, logging, and security-extension work.
