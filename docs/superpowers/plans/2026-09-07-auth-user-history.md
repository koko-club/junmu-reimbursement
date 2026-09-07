# Authentication and Per-User Reimbursement History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add approved self-registration, single-session authentication, private Excel/PDF history, a 30-day recycle bin, and Synology DS925+ Docker delivery to the verified reimbursement generator.

**Architecture:** Preserve `ThreadingHTTPServer`, `generator.py`, `office.py`, `validation.py`, and the verified workbook template. Add focused SQLite, security, user, session, reimbursement-record, and HTTP modules; enforce authentication, role, status, CSRF, and ownership on the server before every protected operation. Develop on macOS and ship a self-contained `linux/amd64` image with `/data` mounted from the NAS.

**Tech Stack:** Python 3.12 standard library, SQLite WAL, `hashlib.scrypt`, `openpyxl`, Pillow, LibreOffice headless, HTML/CSS/vanilla JavaScript, `unittest`, Docker Compose.

---

## File Map

- `app.py`: process entry point, service composition, server and cleanup lifecycle.
- `config.py`: typed JSON configuration plus Docker environment overrides.
- `database.py`: SQLite connections, migrations, transactions, and backup.
- `security.py`: password hashing, application secret, anonymous CSRF signing.
- `users.py`: setup, registration, approval, account status/profile, password operations.
- `sessions.py`: opaque 7-day single-session issue, resolve, replace, and revoke.
- `reimbursements.py`: atomic generation, history ownership, recycle bin, purge.
- `web.py`: explicit routes, parsing, cookies, guards, responses, request logging.
- `generator.py`, `office.py`, `validation.py`: preserved generation behavior with narrow integration changes.
- `templates/{setup,login,register,index,history,admin,change-password}.html`: public and protected pages.
- `static/{common,auth,app,history,admin}.js`, `static/styles.css`: browser behavior and selected left-sidebar UI.
- `backup.py`: consistent SQLite and user-file backup command.
- `Dockerfile`, `compose.yaml`, `.env.example`, `.dockerignore`, `scripts/*.sh`: amd64 Synology package.
- `tests/`: unit, HTTP permission, generation regression, concurrency, backup, frontend, and Docker tests.

---

### Task 1: Copy and lock the verified baseline

**Files:**
- Create: `.gitignore`
- Copy: `app.py`, `generator.py`, `office.py`, `validation.py`, `config.json`, `requirements.txt`
- Copy: `templates/`, `static/`, `resources/`, `tests/`
- Copy: `build_deployment_package.py`, `README_部署说明.md`, and the three existing launchers
- Source: `/Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903`

- [x] **Step 1: Copy only source, resources, and tests**

```bash
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/app.py .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/generator.py .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/office.py .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/validation.py .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/config.json .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/requirements.txt .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/build_deployment_package.py .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/README_部署说明.md .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/start_reimbursement_tool.sh .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/start_reimbursement_tool.command .
cp /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/start_reimbursement_tool.bat .
cp -R /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/templates .
cp -R /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/static .
cp -R /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/resources .
cp -R /Users/koko/Documents/Codex/其他/reimbursement_form_app_20260903/tests .
```

Expected: do not copy `generated/`, `dist/`, `runtime/`, caches, or old planning files. Only the explicitly listed legacy launchers and packager are copied temporarily so the inherited baseline suite can run; Task 13 replaces them.

- [x] **Step 2: Create `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
.DS_Store
.venv/
data/
generated/
dist/
backups/
.env
.superpowers/
```

- [x] **Step 3: Run the inherited suite**

```bash
python3 -m unittest discover -s tests -v
```

Expected: all inherited tests pass; only the existing optional PDF branch may skip when local LibreOffice is unavailable.

- [x] **Step 4: Initialize Git and commit the baseline**

```bash
git init
git add .gitignore app.py generator.py office.py validation.py config.json requirements.txt templates static resources tests docs task_plan.md findings.md progress.md build_deployment_package.py README_部署说明.md start_reimbursement_tool.sh start_reimbursement_tool.command start_reimbursement_tool.bat
git commit -m "chore: preserve reimbursement app baseline"
```

Expected: a root commit in the new project; the old project remains untouched.

---

### Task 2: Add typed configuration and the SQLite schema

**Files:**
- Create: `config.py`
- Create: `database.py`
- Create: `tests/test_database.py`
- Modify: `config.json`
- Modify: `tests/test_config.py`

- [x] **Step 1: Write failing configuration and schema tests**

```python
def test_environment_overrides_docker_values(self):
    config = load_config(self.config_path, {
        "APP_PORT": "8801", "APP_HOST": "0.0.0.0",
        "APP_DATA_DIR": "/data", "APP_SOFFICE_PATH": "/usr/bin/soffice",
    })
    self.assertEqual(config.port, 8801)
    self.assertEqual(config.host, "0.0.0.0")
    self.assertEqual(config.data_dir, Path("/data"))
    self.assertEqual(config.soffice_path, "/usr/bin/soffice")

def test_migrate_is_idempotent_and_enables_safety(self):
    database = Database(self.root / "app.db")
    database.migrate()
    database.migrate()
    with database.connect() as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"users", "sessions", "reimbursements", "app_settings", "schema_migrations"} <= tables)
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
```

- [x] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_config tests.test_database -v
```

Expected: import failures for `config` and `database`.

- [x] **Step 3: Implement configuration**

Create immutable `AppConfig` with `template_path`, `data_dir`, `templates_dir`, `static_dir`, `host`, `port`, `soffice_path`, `max_body_bytes`, `max_concurrent_generations`, and `cookie_secure`. `load_config(path,environ=None)` resolves JSON-relative paths against the config directory and honors `APP_HOST`, `APP_PORT`, `APP_DATA_DIR`, `APP_SOFFICE_PATH`, `APP_MAX_BODY_BYTES`, `APP_MAX_CONCURRENT_GENERATIONS`, and `APP_COOKIE_SECURE`.

Use this local default:

```json
{
  "template_path": "resources/差旅报销单模板.xlsx",
  "data_dir": "data",
  "host": "127.0.0.1",
  "port": 8800,
  "soffice_path": "",
  "max_body_bytes": 10485760,
  "max_concurrent_generations": 2,
  "cookie_secure": false
}
```

Reject ports outside 1–65535 except test port 0, reject non-positive limits, and parse booleans only from `true/false`, `1/0`, or `yes/no` case-insensitively.

- [x] **Step 4: Implement `Database` and migration version 1**

Each connection uses `sqlite3.Row`, foreign keys, 5000 ms busy timeout, and WAL. Transactions commit on success and roll back on every exception. `Database.backup(destination)` uses SQLite's backup API.

Migration version 1 is:

```sql
CREATE TABLE users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
 password_hash BLOB NOT NULL, password_salt BLOB NOT NULL, password_params TEXT NOT NULL,
 real_name TEXT NOT NULL, department TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','user')),
 status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')),
 must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1)),
 created_at TEXT NOT NULL, approved_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE sessions (
 token_hash BLOB PRIMARY KEY,
 user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
 csrf_token TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE reimbursements (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
 reimbursement_date TEXT, display_name TEXT NOT NULL,
 xlsx_path TEXT NOT NULL, pdf_path TEXT NOT NULL,
 created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE INDEX reimbursements_owner_created ON reimbursements(user_id,deleted_at,created_at DESC);
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
INSERT INTO app_settings(key,value) VALUES ('setup_complete','false');
```

- [x] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_config tests.test_database -v
git add config.py database.py config.json tests/test_config.py tests/test_database.py
git commit -m "feat: add persistent application database"
```

Expected: focused tests pass.

---

### Task 3: Add password hashing, application secret, and anonymous CSRF

**Files:**
- Create: `security.py`
- Create: `tests/test_security.py`

- [x] **Step 1: Write failing security tests**

```python
def test_password_hash_uses_random_salts_and_verifies(self):
    hasher = PasswordHasher(n=1024, r=8, p=1)
    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")
    self.assertNotEqual(first.salt, second.salt)
    self.assertTrue(hasher.verify("correct horse battery staple", first))
    self.assertFalse(hasher.verify("wrong password", first))

def test_secret_is_created_once_with_private_permissions(self):
    path = self.root / "app-secret"
    self.assertEqual(load_or_create_secret(path), load_or_create_secret(path))
    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

def test_anonymous_csrf_binds_method_path_and_expiry(self):
    signer = AnonymousCsrfSigner(b"x" * 32)
    token = signer.issue("POST", "/api/register", now=1000)
    self.assertTrue(signer.verify(token, "POST", "/api/register", now=1100))
    self.assertFalse(signer.verify(token, "POST", "/api/setup", now=1100))
    self.assertFalse(signer.verify(token, "POST", "/api/register", now=5000))
```

- [x] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_security -v
```

Expected: the `security` import fails.

- [x] **Step 3: Implement the security primitives**

Create immutable `PasswordMaterial(digest: bytes, salt: bytes, params: str)`. `PasswordHasher` defaults to `n=16384, r=8, p=1`, uses a random 16-byte salt, a 32-byte result, JSON parameters, and `hmac.compare_digest`. Passwords must be strings containing 8–128 Unicode characters.

The hash operation is exactly:

```python
digest = hashlib.scrypt(
    password.encode("utf-8"), salt=salt,
    n=self.n, r=self.r, p=self.p, dklen=32,
)
```

`load_or_create_secret(path)` uses exclusive creation, 32 random bytes, `fsync`, and mode `0600`; concurrent creation reads the winning file. `AnonymousCsrfSigner` signs issue time, method, path, and random nonce with HMAC-SHA256, uses constant-time comparison, and expires tokens after 30 minutes.

- [x] **Step 4: Run tests and commit**

```bash
python3 -m unittest tests.test_security -v
git add security.py tests/test_security.py
git commit -m "feat: add password and csrf primitives"
```

Expected: all security tests pass.

---

### Task 4: Add the user account lifecycle

**Files:**
- Create: `users.py`
- Create: `tests/test_users.py`

- [x] **Step 1: Write failing lifecycle tests**

```python
def test_setup_closes_permanently(self):
    admin = self.users.setup_admin("admin", "administrator1", "管理员", "管理")
    self.assertEqual((admin["role"], admin["status"]), ("admin", "active"))
    with self.assertRaises(SetupClosed):
        self.users.setup_admin("second", "administrator2", "第二人", "管理")

def test_approval_and_rejection_release_username(self):
    pending = self.users.register("Alice", "ordinary-user1", "张三", "综合部")
    self.assertEqual(self.users.approve(self.admin_id, pending["id"])["status"], "active")
    rejected = self.users.register("Bob", "ordinary-user2", "李四", "业务部")
    self.users.reject(self.admin_id, rejected["id"])
    self.assertEqual(self.users.register("bob", "ordinary-user3", "王五", "财务部")["username_key"], "bob")

def test_admin_management_cannot_target_admin(self):
    with self.assertRaises(UserOperationForbidden):
        self.users.set_enabled(self.admin_id, self.admin_id, False)
```

Also test NFKC plus `casefold()` uniqueness, 3–50 character usernames, required real name/department, pending/disabled authentication denial, profile editing, enable/disable, and reset setting `must_change_password=1`.

- [x] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_users -v
```

Expected: `UserService` is missing.

- [x] **Step 3: Implement `UserService`**

Provide these exact methods: `setup_complete()`, `setup_admin(username,password,real_name,department)`, `register(username,password,real_name,department)`, `authenticate(username,password)`, `get(user_id)`, `list_pending(admin_id)`, `list_approved_users(admin_id)`, `approve(admin_id,user_id)`, `reject(admin_id,user_id)`, `update_profile(admin_id,user_id,real_name,department)`, `set_enabled(admin_id,user_id,enabled)`, `reset_password(admin_id,user_id)`, and `change_password(user_id,current_password,new_password)`.

Normalize username keys with:

```python
username_key = unicodedata.normalize("NFKC", username).strip().casefold()
```

Use immediate transactions for setup, approval/rejection, status, and password changes. Each admin operation loads an active admin actor and a `role='user'` target in the same transaction. Translate duplicate keys to `UsernameTaken`. Inject `revoke_sessions(user_id)` and invoke it after disable, reset, and password change. `reset_password()` generates `secrets.token_urlsafe(12)`, stores only its hash, returns plaintext once, and never logs it.

- [x] **Step 4: Run tests and commit**

```bash
python3 -m unittest tests.test_users -v
git add users.py tests/test_users.py
git commit -m "feat: add approved user lifecycle"
```

Expected: all user tests pass.

---

### Task 5: Add opaque single-session authentication

**Files:**
- Create: `sessions.py`
- Create: `tests/test_sessions.py`
- Modify: `users.py`
- Modify: `tests/test_users.py`

- [x] **Step 1: Write failing session tests**

```python
def issue_after_authentication(self, username, password, now):
    authenticated_user = self.users.authenticate(username, password)
    return self.sessions.issue(authenticated_user, now=now)

def test_new_login_replaces_old_session(self):
    first = self.issue_after_authentication("alex", "correct horse battery staple", self.at(0))
    second = self.issue_after_authentication("alex", "correct horse battery staple", self.at(10))
    self.assertIsNone(self.sessions.resolve(first.token, now=self.at(11)))
    self.assertEqual(self.sessions.resolve(second.token, now=self.at(11)).user_id, self.user_id)

def test_session_has_absolute_seven_day_expiry(self):
    issued = self.issue_after_authentication("alex", "correct horse battery staple", self.at(0))
    self.assertIsNotNone(self.sessions.resolve(issued.token, now=self.at(days=6, seconds=86399)))
    self.assertIsNone(self.sessions.resolve(issued.token, now=self.at(days=7)))

def test_disabled_user_is_rejected_even_with_session_row(self):
    issued = self.issue_after_authentication("alex", "correct horse battery staple", self.at(0))
    self.disable_user_directly(self.user_id)
    self.assertIsNone(self.sessions.resolve(issued.token, now=self.at(1)))

def test_stale_authentication_snapshot_cannot_issue_after_reset(self):
    authenticated_user = self.users.authenticate("alex", "correct horse battery staple")
    self.users.reset_password(self.admin_id, self.user_id)
    with self.assertRaises(ValueError):
        self.sessions.issue(authenticated_user, now=self.at(1))
```

Also test that SQLite stores only SHA-256 token hashes, both roles get one session, logout/revoke work, expired rows purge, and logged-in CSRF uses constant-time comparison. Use event/barrier synchronization to verify that an old authentication snapshot cannot issue after a password change/reset or a disable/reenable transition completes.

- [x] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_sessions -v
```

Expected: `SessionService` is missing.

- [x] **Step 3: Implement the session service**

Create immutable `IssuedSession(token,csrf_token,expires_at)` and `AuthenticatedUser(user_id,username,real_name,department,role,must_change_password,csrf_token)` dataclasses. Implement `issue(authenticated_user,now=None)`, `resolve(token,now=None)`, `verify_csrf(user,submitted)`, `revoke_token(token)`, `revoke_user(user_id)`, and `purge_expired(now=None)`. `authenticated_user` is the `User` snapshot returned by `UserService.authenticate()`; `issue_authenticated(authenticated_user,now=None)` is the explicit login-route alias. Do not expose a bare `user_id` signing path.

Generate token and CSRF values with `secrets.token_urlsafe(32)` and store only:

```python
token_hash = hashlib.sha256(token.encode("ascii")).digest()
```

`issue()` deletes the previous row and inserts the replacement in one immediate transaction with `expires_at = now + timedelta(days=7)`. In that same transaction it requires `status = 'active'` and exact equality with the authentication snapshot's `password_version` and `status_version`; an old snapshot must not issue after password change/reset or disable/reenable. `resolve()` joins the active user, deletes expired sessions, and never extends expiry. Wire `SessionService.revoke_user` into `UserService` and test revocation after disable, reset, and self-change.

- [x] **Step 4: Run tests and commit**

```bash
python3 -m unittest tests.test_sessions tests.test_users -v
git add sessions.py users.py tests/test_sessions.py tests/test_users.py
git commit -m "feat: enforce one active session per account"
```

Expected: session and user tests pass.

---

### Task 6: Build guarded HTTP and authentication routes

**Files:**
- Create: `web.py`
- Create: `tests/http_helpers.py`
- Create: `tests/test_web_auth.py`
- Modify: `app.py`
- Modify: `tests/test_server.py`

- [x] **Step 1: Create a cookie-preserving fixture and failing route tests**

`tests/http_helpers.py` exposes a `RunningApp` context manager that creates temporary config/templates/data, starts `create_server()` on port 0, and provides clients backed by `urllib.request.HTTPCookieProcessor`.

```python
def test_uninitialized_app_allows_only_setup_and_health(self):
    self.assertEqual(self.client.get("/healthz").status, 200)
    self.assertEqual(self.client.get("/setup").status, 200)
    self.assertEqual(self.client.get("/login", follow_redirects=False).status, 303)
    self.assertEqual(self.client.get("/register", follow_redirects=False).status, 303)

def test_login_cookie_is_http_only_and_new_login_revokes_old(self):
    self.setup_admin()
    first = self.client.post_json("/api/login", {"username": "admin", "password": "administrator1"})
    self.assertIn("HttpOnly", first.headers["Set-Cookie"])
    self.assertIn("SameSite=Lax", first.headers["Set-Cookie"])
    second_client = self.app.new_client()
    second_client.post_json("/api/login", {"username": "admin", "password": "administrator1"})
    self.assertEqual(self.client.get("/api/session").status, 401)

def test_forced_change_session_cannot_open_application(self):
    self.login_with_temporary_password()
    self.assertEqual(self.client.get("/change-password").status, 200)
    self.assertEqual(self.client.get("/", follow_redirects=False).status, 303)
    self.assertEqual(self.client.get("/api/admin/users").status, 403)
```

Also test anonymous CSRF on setup/login/register, session CSRF on logout/change-password, pending/disabled login messages, cookie deletion, role redirects, body-size checks before parsing, static traversal, and `Secure` only when configured.

- [x] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_web_auth -v
```

Expected: guarded routes are absent.

- [x] **Step 3: Implement `WebApplication`**

Define explicit GET/POST route tables. Before setup completion, allow only `/healthz`, setup GET/POST, and required static assets. After setup, HTML guards use 303 redirects and API guards use JSON 401/403. Apply guards in this order: setup, session, account status, forced password change, role, CSRF for writes, resource ownership.

Use cookie name `reimbursement_session`; set `HttpOnly`, `SameSite=Lax`, `Path=/`, 7-day `Max-Age`, and configurable `Secure`. Send `Cache-Control: no-store` on account pages and JSON. Stream files in 64 KiB chunks. Reject unsupported methods with 405 and unknown routes with 404.

The authentication route set is:

```text
GET  /healthz
GET  /setup
POST /api/setup
GET  /login
POST /api/login
GET  /register
POST /api/register
GET  /api/session
GET  /change-password
POST /api/password/change
POST /api/logout
```

- [x] **Step 4: Compose services in `app.create_server()`**

Load `AppConfig`, create the data directory, migrate the database, load `/data/app-secret`, and compose `PasswordHasher`, `SessionService`, `UserService`, and `WebApplication`. Keep the public signature `create_server(config_path: Path) -> ThreadingHTTPServer`.

The request handler remains thin:

```python
class Handler(BaseHTTPRequestHandler):
    server_version = "ReimbursementTool/2.0"
    def do_GET(self) -> None:
        application.handle_get(self)
    def do_POST(self) -> None:
        application.handle_post(self)
    def log_message(self, format: str, *args: object) -> None:
        return
```

Adapt inherited tests to initialize/login; do not preserve unauthenticated `/generate` or filename-only `/download` compatibility.

- [x] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_web_auth tests.test_server -v
git add app.py web.py tests/http_helpers.py tests/test_web_auth.py tests/test_server.py
git commit -m "feat: protect web routes with sessions"
```

Expected: authentication and adapted server tests pass.

Completion follow-up: Task 6 also implements one shared two-operation scrypt concurrency budget, setup/register per-IP rate limits, and bounded login budgets for canonical account+IP, independent client IP, and a fixed global key. In each 15-minute window, account+IP permits 5 attempts, client IP permits 10, and the global key permits 100. Attacker-controlled login fields are mapped to fixed-length limiter keys, limiter storage is capped at 4096 keys and fails closed when full, and successful login clears only its account+IP key. Request-line plus headers share one absolute monotonic deadline; body reading gets a separate absolute deadline. A deadline-aware raw reader recalculates the remaining budget before every socket read and clears the phase synchronously before the business callback, so later long-running generation work is not covered by parsing deadlines and there is no timer/close race.

---

### Task 7: Implement atomic per-user generation

**Files:**
- Create: `reimbursements.py`
- Create: `tests/test_reimbursements.py`
- Modify: `generator.py`
- Modify: `tests/test_generator.py`
- Modify: `validation.py`
- Modify: `tests/test_validation.py`
- Modify: `tests/test_end_to_end.py`
- Modify: `app.py`

- [ ] **Step 1: Write failing generation tests**

```python
def test_generate_overrides_client_profile_and_records_owner(self):
    payload = valid_payload(department="伪造部门", traveler="伪造姓名")
    record = self.service.generate(self.user, payload, [])
    generated_payload = self.generator.call_args.args[2]
    self.assertEqual(generated_payload["department"], self.user.department)
    self.assertEqual(generated_payload["traveler"], self.user.real_name)
    self.assertEqual(record.user_id, self.user.user_id)

def test_pdf_failure_leaves_no_file_or_history(self):
    self.office.side_effect = OfficeError("conversion failed")
    with self.assertRaises(ReimbursementGenerationError):
        self.service.generate(self.user, valid_payload(), [])
    with self.database.connect() as connection:
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM reimbursements").fetchone()[0], 0)
    self.assertEqual(list((self.data_dir / "users").rglob("*.xlsx")), [])
```

Also test blank reimbursement date, 11 rows, same display name from two users, unsafe returned paths, database insert failure cleanup, and semaphore queuing.

- [ ] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_reimbursements -v
```

Expected: `ReimbursementService` is missing.

- [ ] **Step 3: Make profile injection explicit in validation**

Change `validate_payload` to accept keyword-only trusted `traveler` and `department` overrides after normalizing client data. Preserve existing optional header/detail dates, numeric checks, formula-leading-text rejection, and exactly 11 padded rows.

```python
payload = validate_payload(
    raw_payload,
    traveler=user.real_name,
    department=user.department,
)
```

- [ ] **Step 4: Implement `ReimbursementService`**

Create immutable `ReimbursementRecord` with `id`, `user_id`, `reimbursement_date`, `display_name`, `xlsx_path`, `pdf_path`, `created_at`, and `deleted_at`. Implement `generate(user,raw_payload,screenshots)`, `list_active(user_id)`, `list_trash(user_id)`, and `owned_file(user_id,record_id,kind,include_deleted=False)`. `owned_file()` returns a context-managed `OwnedReimbursementFile` containing the record, artifact kind, display name, size, and an already-open binary stream; callers must close it with `with` and must never reopen a returned path.

`generate()` requires ordinary-user role, creates UUID4 ID and `/data/tmp/<id>`, applies trusted profile fields, and computes a filesystem-safe output stem capped at 150 UTF-8 bytes before entering `BoundedSemaphore(max_concurrent_generations)`. The workbook retains the complete trusted profile value. Anchor data/tmp/users/owner/record directories with `O_DIRECTORY|O_NOFOLLOW` descriptors; create and move record directories only with `dir_fd`-relative operations. Because cross-platform Python cannot atomically create a directory and return its fd, directory-creation failure atomicity is best effort: capture the candidate identity with the first no-follow stat after `mkdir`, require the opened fd identity to match, and retain the UUID entry whenever that first identity check fails or ownership cannot otherwise be proven. Open each output through every directory component with `O_NOFOLLOW`, require a regular single-link file, preserve its `(st_dev,st_ino)` and open descriptor through database commit, and recheck the same identity after PDF export, immediately before move, and after move. Cleanup validates the request-owned directory identity and uses symlink-resistant fd-relative recursion; it never reparses an absolute cleanup path. Then insert only server-controlled relative paths. On failure, remove only artifacts whose identities belong to that call and raise a stable Chinese web error while internal logs retain only the record ID and exception class at this service boundary. Unverified, unrecorded UUID orphan directories remain invisible and may be ignored by Task 8/13 cleanup and backup; automated orphan deletion is outside this task and any future cleanup must use an age threshold plus equivalent identity safety.

- [ ] **Step 5: Run regressions and commit**

```bash
python3 -m unittest tests.test_validation tests.test_generator tests.test_office tests.test_reimbursements -v
git add reimbursements.py validation.py tests/test_reimbursements.py tests/test_validation.py app.py
git commit -m "feat: generate private reimbursement files"
```

Expected: all focused tests pass, including formulas, blank dates, PDF uppercase total, margin, and concurrency cases.

---

### Task 8: Add history, download, recycle-bin, and purge APIs

**Files:**
- Create: `tests/test_web_reimbursements.py`
- Create: `tests/test_cleanup.py`
- Modify: `reimbursements.py`
- Modify: `web.py`
- Modify: `app.py`

Download handlers consume `OwnedReimbursementFile` only inside its context manager, set metadata from that object, and stream `owned.stream` in 64 KiB chunks. They never resolve or reopen a filesystem path after authorization.

- [ ] **Step 1: Write the failing permission matrix**

```python
def test_file_routes_allow_only_owner(self):
    record = self.generate_as(self.alice)
    self.assertEqual(self.alice_client.get(f"/api/reimbursements/{record.id}/pdf").status, 200)
    self.assertEqual(self.bob_client.get(f"/api/reimbursements/{record.id}/pdf").status, 404)
    self.assertEqual(self.admin_client.get(f"/api/reimbursements/{record.id}/pdf").status, 404)
    self.assertEqual(self.anonymous.get(f"/api/reimbursements/{record.id}/pdf").status, 401)

def test_delete_restore_and_permanent_delete(self):
    record = self.generate_as(self.alice)
    self.alice_client.post_json(f"/api/reimbursements/{record.id}/trash", {}, csrf=True)
    self.assertEqual(self.alice_client.get(f"/api/reimbursements/{record.id}/xlsx").status, 404)
    self.alice_client.post_json(f"/api/reimbursements/{record.id}/restore", {}, csrf=True)
    self.assertEqual(self.alice_client.get(f"/api/reimbursements/{record.id}/xlsx").status, 200)
    self.alice_client.post_json(f"/api/reimbursements/{record.id}/trash", {}, csrf=True)
    self.alice_client.post_json(f"/api/reimbursements/{record.id}/purge", {"confirm": True}, csrf=True)
    self.assertFalse((self.data_dir / record.xlsx_path).exists())
```

Also test list scopes, Excel attachment, PDF inline, invalid kind/UUID, missing CSRF, purge without confirmation, expired purge, partial deletion, and stale-session denial.

- [ ] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_web_reimbursements tests.test_cleanup -v
```

Expected: lifecycle routes are absent.

- [ ] **Step 3: Implement lifecycle methods**

Add `trash(user_id,record_id,now=None)`, `restore(user_id,record_id)`, `purge_one(user_id,record_id)`, and `purge_expired(now=None)`. Every query contains both record ID and owner ID. Active downloads require `deleted_at IS NULL`. Permanent deletion only accepts trashed records; validate that resolved paths stay below `/data/users/<user_id>/<record_id>`. If filesystem removal fails, retain the database row for retry. Expired purge selects records older than 30 days and continues after individual errors.

- [ ] **Step 4: Add exact routes**

```text
GET  /api/reimbursements?scope=active
GET  /api/reimbursements?scope=trash
GET  /api/reimbursements/<record_id>/xlsx
GET  /api/reimbursements/<record_id>/pdf
POST /api/reimbursements/<record_id>/trash
POST /api/reimbursements/<record_id>/restore
POST /api/reimbursements/<record_id>/purge
POST /api/reimbursements/generate
```

Another owner's record and a missing record return the same 404. Set `X-Content-Type-Options: nosniff`; Excel uses attachment disposition, PDF uses inline, and the RFC 5987 display filename never chooses a path.

- [ ] **Step 5: Add cleanup lifecycle and commit**

At startup purge expired sessions and reimbursements once. Start one daemon loop using `threading.Event.wait(86400)`; stop and join it during `server_close()`. Tests inject a no-wait cleanup runner instead of sleeping.

```bash
python3 -m unittest tests.test_web_reimbursements tests.test_cleanup tests.test_server -v
git add reimbursements.py web.py app.py tests/test_web_reimbursements.py tests/test_cleanup.py tests/test_server.py
git commit -m "feat: add private history and recycle bin"
```

Expected: permission, cleanup, and server tests pass.

---

### Task 9: Add administration APIs and safe logging

**Files:**
- Modify: `web.py`
- Modify: `app.py`
- Modify: `tests/test_web_auth.py`
- Create: `tests/test_request_security.py`

- [ ] **Step 1: Write failing administration/logging tests and extend hardening regressions**

```python
def test_admin_manages_an_ordinary_user(self):
    pending = self.register("alice", "ordinary-user1", "张三", "综合部")
    self.admin.post_json(f"/api/admin/registrations/{pending['id']}/approve", {}, csrf=True)
    self.admin.post_json(f"/api/admin/users/{pending['id']}/profile", {"real_name": "张三", "department": "财务部"}, csrf=True)
    self.admin.post_json(f"/api/admin/users/{pending['id']}/status", {"enabled": False}, csrf=True)
    reset = self.admin.post_json(f"/api/admin/users/{pending['id']}/reset-password", {}, csrf=True).json()
    self.assertTrue(reset["temporary_password"])
    self.assertNotIn(reset["temporary_password"], self.read_server_log())

def test_csrf_and_size_checks_run_before_domain_logic(self):
    route = "/api/reimbursements/00000000-0000-0000-0000-000000000000/trash"
    self.assertEqual(self.user.post_json(route, {}, csrf=False).status, 403)
    response = self.user.post_raw("/api/reimbursements/generate", b"x" * 1025,
                                  content_type="multipart/form-data; boundary=x",
                                  declared_length=1025, max_body_bytes=1024)
    self.assertEqual(response.status, 413)
```

Also cover reject/re-register, non-admin denial for every admin route, admin-target rejection, malformed input, and secret-free logs. Retain Task 6 regressions for setup/register/login rate limiting and the shared scrypt concurrency budget; those imports and behaviors must already pass when Task 9 starts, and any Task 9 cases extend them as regression coverage rather than expecting them to be absent.

- [ ] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_web_auth tests.test_request_security -v
```

Expected: existing Task 6 throttle/scrypt regressions pass; administration routes and request logging tests fail.

- [ ] **Step 3: Implement exact administration routes**

```text
GET  /api/admin/registrations
POST /api/admin/registrations/<user_id>/approve
POST /api/admin/registrations/<user_id>/reject
GET  /api/admin/users
POST /api/admin/users/<user_id>/profile
POST /api/admin/users/<user_id>/status
POST /api/admin/users/<user_id>/reset-password
POST /api/password/change
POST /api/logout
```

Every admin write requires active admin role and CSRF. User-management routes reject `role='admin'` targets. Password reset returns plaintext once under `Cache-Control: no-store`; no database or log field contains it.

- [ ] **Step 4: Add request IDs and safe JSON logs**

Create `secrets.token_hex(8)` request ID per request. Log timestamp, request ID, remote IP, known user ID, method, named route, status, duration, and exception class. Never log headers, Cookie, body, passwords, temporary passwords, tokens, form data, or full file paths.

Reuse the lock-protected setup/register/login throttling and shared scrypt budget completed in Task 6; do not add a second limiter or reimplement those behaviors in Task 9.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_web_auth tests.test_request_security tests.test_users tests.test_sessions -v
git add web.py app.py tests/test_web_auth.py tests/test_request_security.py
git commit -m "feat: expose secure account administration"
```

Expected: account and request-hardening tests pass.

---

### Task 10: Build setup, login, registration, and password pages

**Files:**
- Create: `templates/setup.html`
- Create: `templates/login.html`
- Create: `templates/register.html`
- Create: `templates/change-password.html`
- Create: `static/common.js`
- Create: `static/auth.js`
- Modify: `static/styles.css`
- Create: `tests/test_frontend.py`

- [ ] **Step 1: Write failing frontend contracts**

```python
def test_auth_pages_have_fields_without_password_defaults(self):
    self.assert_page_fields("setup.html", "username", "password", "confirm-password", "real-name", "department")
    self.assert_page_fields("register.html", "username", "password", "confirm-password", "real-name", "department")
    self.assert_page_fields("login.html", "username", "password")
    for page in ("setup.html", "register.html", "login.html"):
        self.assertNotRegex(self.read(page), r'type="password"[^>]+value=')

def test_common_fetch_adds_csrf_and_handles_replaced_session(self):
    script = self.read_static("common.js")
    self.assertIn("X-CSRF-Token", script)
    self.assertIn("credentials: 'same-origin'", script)
    self.assertIn("response.status === 401", script)
    self.assertIn("window.location.assign('/login')", script)
```

Also assert labels, autocomplete values, confirmation input, pending/disabled messages, `textContent` for server errors, focus styles, and no password/browser-storage persistence.

- [ ] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_frontend -v
```

Expected: new templates and scripts are absent.

- [ ] **Step 3: Implement account forms and submission**

Each page loads shared CSS/common/auth scripts, uses `<form novalidate>`, and pairs every control with a `<label for>`. Setup and registration compare passwords in the browser and still rely on server checks. The GET route embeds an action-bound anonymous CSRF token in `data-csrf`.

Use this request shape:

```javascript
const response = await fetch(endpoint, {
  method: 'POST',
  credentials: 'same-origin',
  headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.dataset.csrf},
  body: JSON.stringify(Object.fromEntries(new FormData(form)))
});
```

Disable the submit button only while awaiting the response. Render errors with `textContent`. Never put a password in a URL, browser storage, or retained DOM after successful navigation.

- [ ] **Step 4: Add account-page styling and commit**

Use white/light gray surfaces, dark text, green primary actions, red destructive/error states, maximum 6px radius, stable 40px controls, visible `:focus-visible`, and responsive padding. Do not use gradients, decorative blobs, nested cards, or oversized headings.

```bash
python3 -m unittest tests.test_frontend tests.test_web_auth -v
git add templates/setup.html templates/login.html templates/register.html templates/change-password.html static/common.js static/auth.js static/styles.css tests/test_frontend.py
git commit -m "feat: add registration and login pages"
```

Expected: frontend contracts and authentication routes pass.

---

### Task 11: Integrate the form with the selected left sidebar

**Files:**
- Modify: `templates/index.html`
- Modify: `static/app.js`
- Modify: `static/common.js`
- Modify: `static/styles.css`
- Modify: `tests/test_frontend.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing shell and form tests**

```python
def test_user_shell_has_sidebar_and_locked_profile(self):
    html = self.read("index.html")
    for href in ('href="/"', 'href="/history"', 'href="/trash"'):
        self.assertIn(href, html)
    self.assertRegex(html, r'id="traveler"[^>]+readonly')
    self.assertRegex(html, r'id="department"[^>]+readonly')
    self.assertIn('aria-controls="app-sidebar"', html)
    self.assertEqual(html.count('class="detail-row"'), 11)

def test_generation_uses_private_api_and_pdf_new_tab(self):
    script = self.read_static("app.js")
    self.assertIn("/api/reimbursements/generate", script)
    self.assertIn("apiFetch", script)
    self.assertIn("link.target = '_blank'", script)
    self.assertIn("link.rel = 'noopener'", script)
```

- [ ] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_frontend.FrontendContractTest -v
```

Expected: sidebar, readonly profile, and protected-endpoint checks fail.

- [ ] **Step 3: Adapt the form without changing its data contract**

Wrap the existing form in the selected 216px left-sidebar shell. Keep exactly 11 detail rows, optional date inputs, all numeric fields, image preview, reset, and generated links. Remove the hard-coded department. Load `/api/session`, fill traveler/department from the profile, and keep them readonly; the server remains authoritative.

Submit with:

```javascript
const response = await apiFetch('/api/reimbursements/generate', {
  method: 'POST',
  body: body
});
```

`apiFetch()` adds session CSRF to non-GET requests, redirects 401 to `/login`, redirects forced-change responses to `/change-password`, and preserves form state on errors.

- [ ] **Step 4: Add responsive sidebar and commit**

Below 900px the sidebar becomes an off-canvas menu controlled by an icon button with `aria-expanded`; overlay and Escape close it. The reimbursement table retains horizontal scrolling and a stable minimum width.

```bash
python3 -m unittest tests.test_frontend tests.test_server tests.test_validation tests.test_generator -v
git add templates/index.html static/app.js static/common.js static/styles.css tests/test_frontend.py tests/test_server.py
git commit -m "feat: add authenticated reimbursement workspace"
```

Expected: frontend, server, validation, and generator tests pass.

---

### Task 12: Build history, recycle-bin, and administrator pages

**Files:**
- Create: `templates/history.html`
- Create: `templates/admin.html`
- Create: `static/history.js`
- Create: `static/admin.js`
- Modify: `static/styles.css`
- Modify: `tests/test_frontend.py`

- [ ] **Step 1: Write failing UI contracts**

```python
def test_history_has_active_and_trash_actions(self):
    html = self.read("history.html")
    script = self.read_static("history.js")
    self.assertIn('data-view="active"', html)
    self.assertIn('data-view="trash"', html)
    self.assertIn("window.open(pdfUrl, '_blank', 'noopener')", script)
    self.assertIn("/restore", script)
    self.assertIn("/purge", script)

def test_admin_has_no_reimbursement_access(self):
    combined = self.read("admin.html") + self.read_static("admin.js")
    self.assertIn("待审批", combined)
    self.assertIn("用户管理", combined)
    self.assertIn("reset-password", combined)
    self.assertNotIn("/api/reimbursements", combined)
```

Also assert table headings, empty states, pending badge, remaining trash days, labeled dialogs, DOM `textContent`, no nested cards, profile editing, and enable/disable actions.

- [ ] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_frontend -v
```

Expected: history and admin assets are absent.

- [ ] **Step 3: Implement history and recycle bin**

Choose `scope=active` for `/history` and `scope=trash` for `/trash`. Build rows with DOM APIs, format UTC using `Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai'})`, and use server-provided `purge_at` for remaining days. Active actions: Excel attachment, PDF new tab, move to trash. Trash actions: restore and permanent delete. Permanent delete uses a native labeled `<dialog>` and sends `{"confirm":true}` only after confirmation.

- [ ] **Step 4: Implement approval and user management**

Load pending applications and approved users in parallel. Approval/rejection updates the pending badge. Profile edit uses a labeled dialog. Disable requires confirmation; enable does not. Password reset displays the returned temporary password once with a copy button; closing the dialog clears its text node. Admin assets contain no reimbursement-list/download request.

- [ ] **Step 5: Run tests and commit**

```bash
python3 -m unittest tests.test_frontend tests.test_web_auth tests.test_web_reimbursements -v
git add templates/history.html templates/admin.html static/history.js static/admin.js static/styles.css tests/test_frontend.py
git commit -m "feat: add history recycle bin and admin pages"
```

Expected: frontend and backing API tests pass.

---

### Task 13: Add backups and Docker/Synology packaging

**Files:**
- Create: `backup.py`
- Create: `tests/test_backup.py`
- Create: `tests/test_docker_assets.py`
- Create: `.dockerignore`
- Create: `Dockerfile`
- Create: `compose.yaml`
- Create: `.env.example`
- Create: `scripts/build-docker.sh`
- Create: `scripts/export-docker.sh`
- Create: `scripts/backup.sh`
- Create: `scripts/package-synology.sh`
- Modify: `README_部署说明.md`
- Modify: `start_reimbursement_tool.sh`
- Modify: `start_reimbursement_tool.command`
- Modify: `tests/test_deployment.py`
- Delete: `build_deployment_package.py`
- Delete: `start_reimbursement_tool.bat`

- [ ] **Step 1: Write failing backup and Docker contracts**

```python
def test_backup_contains_database_secret_and_user_files(self):
    archive = create_backup(self.data_dir, self.backup_dir, now=self.fixed_now)
    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
    self.assertIn("database/app.db", names)
    self.assertIn("app-secret", names)
    self.assertIn("users/1/record/reimbursement.xlsx", names)

def test_compose_targets_ds925_and_port_8800(self):
    compose = (APP_DIR / "compose.yaml").read_text(encoding="utf-8")
    self.assertIn("platform: linux/amd64", compose)
    self.assertIn('"8800:8800"', compose)
    self.assertIn("/data", compose)
    self.assertIn("restart: unless-stopped", compose)
```

Also assert LibreOffice/Noto CJK installation, non-root execution or Compose UID mapping, health check, build-context exclusions, and Chinese DS925+ import instructions.

- [ ] **Step 2: Verify failure**

```bash
python3 -m unittest tests.test_backup tests.test_docker_assets -v
```

Expected: backup and Docker files are absent.

- [ ] **Step 3: Implement `backup.py`**

`create_backup(data_dir,backup_dir,now=None)` creates `reimbursement-YYYYMMDDTHHMMSSZ.tar.gz`. It uses `Database.backup()` for `database/app.db`, copies `app-secret` and `users/`, writes a SHA-256 manifest, creates the archive at a temporary path with mode `0600`, then atomically renames it. Reject missing required inputs and paths that escape the configured roots.

The call sequence is:

```python
database.backup(staging / "database" / "app.db")
copy_checked(data_dir / "app-secret", staging / "app-secret")
copy_tree_checked(data_dir / "users", staging / "users")
write_manifest(staging)
make_atomic_tar(staging, archive)
```

- [ ] **Step 4: Create the image and Compose definition**

Base `Dockerfile` on `python:3.12-slim-bookworm`. Install `libreoffice-calc`, `libreoffice-core`, `fontconfig`, and `fonts-noto-cjk` without recommended packages; install `requirements.txt`; copy the app; set `HOME=/tmp/app-home`; expose 8800; use an unprivileged image user; run `python app.py`.

Compose must contain:

```yaml
services:
  reimbursement:
    image: reimbursement-system:latest
    platform: linux/amd64
    user: "${PUID:-1000}:${PGID:-1000}"
    environment:
      APP_HOST: 0.0.0.0
      APP_PORT: 8800
      APP_DATA_DIR: /data
      APP_SOFFICE_PATH: /usr/bin/soffice
      APP_COOKIE_SECURE: "false"
      TZ: Asia/Shanghai
    ports:
      - "8800:8800"
    volumes:
      - "${DATA_PATH:-./data}:/data"
      - "${BACKUP_PATH:-./backups}:/backups"
    restart: unless-stopped
```

Add a health check for `http://127.0.0.1:8800/healthz` using Python `urllib.request`.

- [ ] **Step 5: Create exact scripts**

- `build-docker.sh`: build `linux/amd64` as `reimbursement-system:latest` with `docker buildx build --load`.
- `export-docker.sh`: save and gzip the image as `dist/reimbursement-system-linux-amd64.tar.gz`.
- `backup.sh`: stop only service `reimbursement`, install a restart trap, run a one-shot `python backup.py --data-dir /data --backup-dir /backups`, restart, and verify health.
- `package-synology.sh`: require the image archive and package it with Compose, `.env.example`, backup script, and Chinese README.

Each script uses `set -eu`, resolves its own project directory, quotes paths, and rejects empty or broad data/output paths.

Remove the obsolete macOS-arm portable packager and Windows launcher so the delivered project has one unambiguous deployment route. Keep the Mac shell and `.command` launchers, but simplify them to select a Python with `openpyxl`/Pillow, run `app.py`, and open `http://127.0.0.1:8800`; do not reference bundled Codex runtime paths. Replace the old deployment tests with contracts for the Mac launcher and new Docker/Synology assets.

- [ ] **Step 6: Write Chinese deployment instructions**

Document Mac startup, Docker build/export, Container Manager image import, Compose project creation, `DATA_PATH=/volume1/docker/reimbursement/data`, backup path, PUID/PGID, folder permissions, port 8800 firewall, first administrator, scheduled backup, upgrade/rollback, health/log troubleshooting, and deliberate non-migration of old `generated/` files.

- [ ] **Step 7: Run tests and commit**

```bash
python3 -m unittest tests.test_backup tests.test_docker_assets tests.test_config -v
git add -A
git commit -m "feat: package system for Synology"
```

Expected: backup, Docker, and configuration tests pass.

---

### Task 14: Complete end-to-end and visual verification

**Files:**
- Modify: `tests/test_end_to_end.py`
- Create: `tests/test_concurrency.py`
- Create: `tests/test_pdf_regression.py`
- Modify: `README_部署说明.md`
- Modify: `progress.md`

- [ ] **Step 1: Extend the authenticated end-to-end flow**

Test setup, register, approve, login, generate, download both files, trash, and restore through HTTP with real template/generator. Assert workbook values. Run real PDF assertions when LibreOffice exists; only PDF-specific host assertions may skip otherwise.

```python
admin.setup("admin", "administrator1", "管理员", "管理")
registration = user.register("alice", "ordinary-user1", "张三", "综合部")
admin.login("admin", "administrator1")
admin.approve(registration["id"])
user.login("alice", "ordinary-user1")
record = user.generate(complete_payload())
user.trash(record["id"])
user.restore(record["id"])
```

- [ ] **Step 2: Add concurrent generation verification**

Synchronize two approved users' generate requests with `threading.Barrier`. Assert each workbook contains only its owner's profile, owner directories differ, both PDFs exist, and no LibreOffice conversion/profile or `/data/tmp/<id>` directory remains.

- [ ] **Step 3: Add PDF regressions**

For known data, assert factor-1 formula, blank date, top margin, uppercase total text, page count, and a nonblank rendered page region:

```python
self.assertEqual(sheet["G9"].value, '=IF(F9*1=0,"",F9*1)')
self.assertIsNone(sheet["A9"].value)
self.assertEqual(sheet.page_margins.top, 0.1)
self.assertIn("元", extracted_pdf_text)
self.assertGreater(rendered_nonwhite_pixel_ratio, 0.05)
```

Use Poppler when available on Mac. The Docker run must execute LibreOffice/font/PDF checks without skipping.

- [ ] **Step 4: Run the complete host suite**

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass; only explicitly dependency-gated PDF rendering checks may skip on the host.

- [ ] **Step 5: Run browser QA on both viewport sizes**

Start `python3 app.py` and inspect `http://127.0.0.1:8800`. Verify setup, registration/pending state, approval badge, session replacement, forced password change, locked profile, generation, PDF new tab, history, trash, restore, permanent-delete confirmation, disable, and logout. Capture desktop and narrow screenshots of login, form, history, and admin pages; reject overlap, clipping, inaccessible controls, or incoherent mobile sidebar state.

- [ ] **Step 6: Build and test the amd64 container**

```bash
./scripts/build-docker.sh
docker compose up -d
docker compose ps
docker compose exec reimbursement python -m unittest discover -s tests -v
docker compose restart reimbursement
```

Expected: healthy on 8800; tests and PDF checks pass; setup/users/history survive restart.

- [ ] **Step 7: Verify backup restore and package**

```bash
./scripts/backup.sh
./scripts/export-docker.sh
./scripts/package-synology.sh
```

Restore into a fresh temporary Compose data directory and unused host port; verify admin login, approved user, history metadata, Excel, and PDF. Never overwrite primary test data. Expected: one Synology archive in `dist/` containing the amd64 image and deployment resources.

- [ ] **Step 8: Final verification and commit**

```bash
python3 -m unittest discover -s tests -v
git status --short
git add tests/test_end_to_end.py tests/test_concurrency.py tests/test_pdf_regression.py README_部署说明.md progress.md
git commit -m "test: verify authenticated workflow"
```

Expected: all tests pass and the worktree is clean after commit.

---

## Final Acceptance Checklist

- [ ] Old project remains unchanged; old unowned files are not migrated.
- [ ] One-time setup closes permanently; registration requires approval and rejection releases username.
- [ ] Every account has one absolute 7-day session; replacement, logout, password operations, and disable revoke it.
- [ ] Passwords use salted `scrypt`; temporary passwords are shown once and force a change.
- [ ] Every write has CSRF protection and every resource route enforces status, role, and ownership server-side.
- [ ] Account profile overrides client name/department and both fields remain locked in the form.
- [ ] Each user can list, download, trash, restore, and purge only their own records.
- [ ] Admin pages and APIs expose no ordinary-user reimbursement files.
- [ ] Trash purges after 30 days and failed deletion remains retryable.
- [ ] Existing formulas, blank dates, receipt behavior, uppercase total, screenshots, and PDF margins remain correct.
- [ ] Concurrent users cannot cross profiles, files, records, or LibreOffice work directories.
- [ ] Mac service runs on 8800; `linux/amd64` Docker stays healthy and persists `/data`.
- [ ] Backup/restore passes before the Synology package is delivered.
