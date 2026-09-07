from __future__ import annotations

import hashlib
import inspect
import threading
import unittest
from unittest import mock

from tests.http_helpers import RunningApp
import web
from web import AttemptRateLimiter


ADMIN_PASSWORD = "administrator1"
USER_PASSWORD = "correct horse battery staple"
EXPECTED_LOGIN_IP_RATE_LIMIT_ATTEMPTS = 10
EXPECTED_LOGIN_GLOBAL_RATE_LIMIT_ATTEMPTS = 100


class AttemptRateLimiterTest(unittest.TestCase):
    def test_window_is_injected_and_expired_entries_are_lazily_removed(self):
        now = [100.0]
        limiter = AttemptRateLimiter(limit=2, window_seconds=10, clock=lambda: now[0])
        self.assertIsNone(limiter.reserve(("register", "127.0.0.1")))
        self.assertIsNone(limiter.reserve(("register", "127.0.0.1")))
        self.assertEqual(limiter.reserve(("register", "127.0.0.1")), 10)
        now[0] = 111.0
        self.assertIsNone(limiter.reserve(("register", "127.0.0.1")))

    def test_concurrent_reservations_keep_timestamps_in_clock_order(self):
        first_clock_called = threading.Event()
        second_reserved = threading.Event()

        def clock():
            if threading.current_thread().name == "first-reservation":
                first_clock_called.set()
                second_reserved.wait(timeout=0.1)
                return 100.0
            if threading.current_thread().name == "second-reservation":
                return 101.0
            return 102.0

        limiter = AttemptRateLimiter(limit=2, window_seconds=10, clock=clock)

        first = threading.Thread(
            name="first-reservation", target=lambda: limiter.reserve("login")
        )

        def reserve_second():
            limiter.reserve("login")
            second_reserved.set()

        second = threading.Thread(name="second-reservation", target=reserve_second)
        first.start()
        self.assertTrue(first_clock_called.wait(timeout=1))
        second.start()
        first.join(timeout=1)
        second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(limiter.reserve("login"), 8)

    def test_multi_key_reservation_is_atomic_across_distinct_limits(self):
        limiter = AttemptRateLimiter(limit=5, window_seconds=10)
        reservations = (("account", 1), ("ip", 2), ("global", 3))

        self.assertTrue(hasattr(limiter, "reserve_many"))
        self.assertIsNone(limiter.reserve_many(reservations))
        self.assertEqual(limiter.reserve_many(reservations), 10)
        self.assertEqual(len(limiter._attempts["ip"]), 1)
        self.assertEqual(len(limiter._attempts["global"]), 1)

    def test_capacity_is_bounded_and_fails_closed_under_concurrency(self):
        max_keys = 8
        self.assertEqual(getattr(web, "RATE_LIMIT_MAX_KEYS", None), 4096)
        self.assertIn("max_keys", inspect.signature(AttemptRateLimiter).parameters)
        limiter = AttemptRateLimiter(
            limit=2, window_seconds=10, max_keys=max_keys
        )
        start = threading.Barrier(17)
        results = []
        results_lock = threading.Lock()

        def reserve(index):
            start.wait(timeout=1)
            result = limiter.reserve(("login", index))
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=reserve, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        start.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(result is None for result in results), max_keys)
        self.assertEqual(sum(result is not None for result in results), 16 - max_keys)
        self.assertEqual(len(limiter._attempts), max_keys)


class WebAuthenticationTest(unittest.TestCase):
    def setUp(self):
        self.running = RunningApp().__enter__()
        self.client = self.running.client

    def tearDown(self):
        self.running.__exit__(None, None, None)

    def setup_admin(self):
        return self.client.post_json(
            "/api/setup",
            {
                "username": "admin",
                "password": ADMIN_PASSWORD,
                "real_name": "管理员",
                "department": "财务部",
            },
        )

    def register(self, username="alice", password=USER_PASSWORD):
        return self.client.post_json(
            "/api/register",
            {
                "username": username,
                "password": password,
                "real_name": "张三",
                "department": "技术部",
            },
        )

    def approve_user(self, username="alice"):
        admin = self.running.users.authenticate("admin", ADMIN_PASSWORD)
        pending = next(user for user in self.running.users.list_pending(admin.id) if user.username == username)
        return self.running.users.approve(admin.id, pending.id)

    def test_uninitialized_app_allows_only_setup_health_and_static(self):
        self.assertEqual(self.client.get("/healthz").status, 200)
        self.assertEqual(self.client.get("/setup").status, 200)
        self.assertEqual(self.client.get("/static/app.js").status, 200)
        for path in ("/", "/login", "/register", "/change-password"):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status, 303)
                self.assertEqual(response.headers["Location"], "/setup")
        self.assertEqual(self.client.get("/api/session").status, 403)

    def test_anonymous_csrf_is_required_and_bound_to_each_action(self):
        missing = self.client.post_json(
            "/api/setup",
            {"username": "admin", "password": ADMIN_PASSWORD, "real_name": "A", "department": "D"},
            csrf=False,
        )
        self.assertEqual(missing.status, 403)
        setup_token = self.client.csrf_for("/api/setup")
        completed = self.client.post_json(
            "/api/setup",
            {"username": "admin", "password": ADMIN_PASSWORD, "real_name": "A", "department": "D"},
            csrf=setup_token,
        )
        self.assertEqual(completed.status, 201)

        login_token = self.client.csrf_for("/api/login")
        bound = self.client.post_json(
            "/api/register",
            {"username": "alice", "password": USER_PASSWORD, "real_name": "A", "department": "D"},
            csrf=login_token,
        )
        self.assertEqual(bound.status, 403)
        self.assertEqual(self.client.post_json(
            "/api/login", {"username": "admin", "password": ADMIN_PASSWORD}, csrf=False
        ).status, 403)
        self.assertEqual(self.register().status, 201)

    def test_setup_closes_after_first_admin(self):
        token = self.client.csrf_for("/api/setup")
        self.assertEqual(self.setup_admin().status, 201)
        page = self.client.get("/setup", follow_redirects=False)
        self.assertEqual((page.status, page.headers["Location"]), (303, "/login"))
        second = self.client.post_json(
            "/api/setup",
            {"username": "other", "password": ADMIN_PASSWORD, "real_name": "B", "department": "D"},
            csrf=token,
        )
        self.assertEqual(second.status, 403)

    def test_login_cookie_attributes_and_new_login_replaces_old_session(self):
        self.setup_admin()
        first = self.client.post_json(
            "/api/login", {"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(first.status, 200)
        cookie = first.headers["Set-Cookie"]
        for attribute in (
            "reimbursement_session=", "HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=604800"
        ):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(self.client.get("/api/session").status, 200)

        second_client = self.running.new_client()
        self.assertEqual(second_client.post_json(
            "/api/login", {"username": "admin", "password": ADMIN_PASSWORD}
        ).status, 200)
        self.assertEqual(self.client.get("/api/session").status, 401)
        self.assertEqual(second_client.get("/api/session").status, 200)

    def test_secure_cookie_is_only_enabled_by_configuration(self):
        self.running.__exit__(None, None, None)
        self.running = RunningApp(cookie_secure=True).__enter__()
        self.client = self.running.client
        self.setup_admin()
        response = self.client.post_json(
            "/api/login", {"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertIn("Secure", response.headers["Set-Cookie"])

    def test_pending_disabled_and_unknown_accounts_share_login_error(self):
        self.setup_admin()
        registration_client = self.running.new_client()
        registration_client.post_json(
            "/api/register",
            {
                "username": "alice",
                "password": USER_PASSWORD,
                "real_name": "张三",
                "department": "技术部",
            },
        )
        pending = self.client.post_json(
            "/api/login", {"username": "alice", "password": USER_PASSWORD}
        )
        approved = self.approve_user()
        admin = self.running.users.authenticate("admin", ADMIN_PASSWORD)
        self.running.users.set_enabled(admin.id, approved.id, False)
        disabled = self.client.post_json(
            "/api/login", {"username": "alice", "password": USER_PASSWORD}
        )
        unknown = self.client.post_json(
            "/api/login", {"username": "nobody", "password": USER_PASSWORD}
        )
        self.assertEqual((pending.status, disabled.status, unknown.status), (401, 401, 401))
        self.assertEqual(pending.body, disabled.body)
        self.assertEqual(disabled.body, unknown.body)
        self.assertNotIn(b"pending", pending.body.lower())
        self.assertNotIn(b"disabled", disabled.body.lower())

    def test_malformed_login_credentials_still_run_one_password_verification(self):
        self.setup_admin()
        token = self.client.csrf_for("/api/login")
        hasher = self.running.users._password_hasher
        payloads = (
            {"password": USER_PASSWORD},
            {"username": None, "password": USER_PASSWORD},
            {"username": "admin", "password": None},
            {"username": ["admin"], "password": USER_PASSWORD},
        )
        with mock.patch.object(hasher, "verify", wraps=hasher.verify) as verify:
            for payload in payloads:
                with self.subTest(payload=payload):
                    before = verify.call_count
                    response = self.client.post_json("/api/login", payload, csrf=token)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(verify.call_count, before + 1)

    def test_malformed_login_bucket_does_not_collide_with_a_valid_username(self):
        self.client.post_json(
            "/api/setup",
            {
                "username": "<invalid>", "password": ADMIN_PASSWORD,
                "real_name": "A", "department": "D",
            },
        )
        token = self.client.csrf_for("/api/login")
        for _ in range(5):
            self.assertEqual(
                self.client.post_json(
                    "/api/login", {"username": None, "password": USER_PASSWORD}, csrf=token
                ).status,
                401,
            )
        self.assertEqual(
            self.client.post_json(
                "/api/login",
                {"username": "<invalid>", "password": ADMIN_PASSWORD},
                csrf=token,
            ).status,
            200,
        )

    def test_setup_rate_limit_stops_before_domain_work(self):
        token = self.client.csrf_for("/api/setup")
        payload = {
            "username": "admin", "password": ADMIN_PASSWORD,
            "real_name": "A", "department": "D",
        }
        with mock.patch.object(
            self.running.users, "setup_admin", side_effect=ValueError("simulated rejection")
        ) as setup:
            for _ in range(5):
                self.assertEqual(self.client.post_json("/api/setup", payload, csrf=token).status, 400)
            limited = self.client.post_json("/api/setup", payload, csrf=token)
        self.assertEqual(limited.status, 429)
        self.assertEqual(setup.call_count, 5)
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)

    def test_register_rate_limit_is_separate_and_stops_before_hashing(self):
        self.setup_admin()
        token = self.client.csrf_for("/api/register")
        with mock.patch.object(self.running.users, "register", wraps=self.running.users.register) as register:
            for index in range(5):
                response = self.client.post_json(
                    "/api/register",
                    {
                        "username": f"user{index}", "password": USER_PASSWORD,
                        "real_name": "A", "department": "D",
                    },
                    csrf=token,
                )
                self.assertEqual(response.status, 201)
            limited = self.client.post_json(
                "/api/register",
                {"username": "user5", "password": USER_PASSWORD, "real_name": "A", "department": "D"},
                csrf=token,
            )
        self.assertEqual(limited.status, 429)
        self.assertEqual(register.call_count, 5)
        self.assertEqual(
            self.client.post_json(
                "/api/login", {"username": "admin", "password": ADMIN_PASSWORD}
            ).status,
            200,
        )

    def test_login_rate_limit_normalizes_username_and_does_not_pollute_other_keys(self):
        self.setup_admin()
        hasher = self.running.users._password_hasher
        with mock.patch.object(hasher, "verify", wraps=hasher.verify) as verify:
            for username in (" ADMIN ", "admin", "ＡＤＭＩＮ", "Admin", "admin"):
                response = self.client.post_json(
                    "/api/login", {"username": username, "password": "wrong-password"}
                )
                self.assertEqual(response.status, 401)
            limited = self.client.post_json(
                "/api/login", {"username": "admin", "password": "wrong-password"}
            )
            other_key = self.client.post_json(
                "/api/login", {"username": "someone-else", "password": "wrong-password"}
            )
        self.assertEqual(limited.status, 429)
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)
        self.assertEqual(other_key.status, 401)
        self.assertEqual(verify.call_count, 6)

    def test_login_rotating_usernames_is_stopped_by_independent_ip_budget(self):
        self.assertEqual(
            getattr(web, "LOGIN_IP_RATE_LIMIT_ATTEMPTS", None),
            EXPECTED_LOGIN_IP_RATE_LIMIT_ATTEMPTS,
        )
        self.setup_admin()
        token = self.client.csrf_for("/api/login")
        hasher = self.running.users._password_hasher

        with mock.patch.object(hasher, "verify", wraps=hasher.verify) as verify:
            statuses = [
                self.client.post_json(
                    "/api/login",
                    {"username": f"missing-{index}", "password": "wrong-password"},
                    csrf=token,
                ).status
                for index in range(12)
            ]

        self.assertEqual(
            statuses,
            [401] * EXPECTED_LOGIN_IP_RATE_LIMIT_ATTEMPTS
            + [429] * (12 - EXPECTED_LOGIN_IP_RATE_LIMIT_ATTEMPTS),
        )
        self.assertEqual(verify.call_count, EXPECTED_LOGIN_IP_RATE_LIMIT_ATTEMPTS)

    def test_login_global_budget_is_shared_across_client_ips(self):
        self.setup_admin()
        token = self.client.csrf_for("/api/login")
        application = self.running.server.application
        hasher = self.running.users._password_hasher

        with mock.patch("web.LOGIN_GLOBAL_RATE_LIMIT_ATTEMPTS", 3, create=True), mock.patch(
            "web.LOGIN_IP_RATE_LIMIT_ATTEMPTS", 100, create=True
        ), mock.patch.object(
            application,
            "_client_ip",
            side_effect=[f"192.0.2.{index}" for index in range(1, 5)],
        ), mock.patch.object(hasher, "verify", wraps=hasher.verify) as verify:
            statuses = [
                self.client.post_json(
                    "/api/login",
                    {"username": f"missing-{index}", "password": "wrong-password"},
                    csrf=token,
                ).status
                for index in range(4)
            ]

        self.assertEqual(statuses, [401, 401, 401, 429])
        self.assertEqual(verify.call_count, 3)

    def test_invalid_login_usernames_share_one_fixed_short_limiter_sentinel(self):
        self.setup_admin()
        token = self.client.csrf_for("/api/login")
        hasher = self.running.users._password_hasher
        huge_username = "x" * 900_000

        with mock.patch.object(hasher, "verify", wraps=hasher.verify) as verify:
            for username in (None, huge_username, "x" * 51):
                with self.subTest(username_type=type(username).__name__):
                    response = self.client.post_json(
                        "/api/login",
                        {"username": username, "password": "wrong-password"},
                        csrf=token,
                    )
                    self.assertEqual(response.status, 401)

        login_keys = [
            key
            for key in self.running.server.application.rate_limiter._attempts
            if isinstance(key, tuple) and str(key[0]).startswith("login")
        ]

        def strings(value):
            if isinstance(value, str):
                return [value]
            if isinstance(value, tuple):
                return [part for item in value for part in strings(item)]
            return []

        stored_strings = [part for key in login_keys for part in strings(key)]
        self.assertLessEqual(max(len(part) for part in stored_strings), 64)
        account_keys = [key for key in login_keys if key[0] == "login-account-ip"]
        self.assertEqual(len(account_keys), 1)
        self.assertEqual(account_keys[0][1], ("invalid", "<invalid>"))
        self.assertEqual(verify.call_count, 3)

    def test_valid_expanding_username_does_not_share_invalid_limiter_identity(self):
        username = "ß" * 50
        self.setup_admin()
        self.assertEqual(self.register(username=username).status, 201)
        self.approve_user(username=username)
        token = self.client.csrf_for("/api/login")
        limiter_identity = self.running.server.application._login_username_key(username)
        self.assertEqual(
            limiter_identity,
            ("valid-sha256", hashlib.sha256(("ss" * 50).encode("utf-8")).hexdigest()),
        )
        self.assertEqual(len(limiter_identity[1]), 64)

        for _ in range(5):
            self.assertEqual(
                self.client.post_json(
                    "/api/login",
                    {"username": None, "password": "wrong-password"},
                    csrf=token,
                ).status,
                401,
            )

        response = self.client.post_json(
            "/api/login",
            {"username": username, "password": USER_PASSWORD},
            csrf=token,
        )
        self.assertEqual(response.status, 200)

    def test_successful_login_clears_only_account_key_not_ip_or_global_budget(self):
        self.assertEqual(
            getattr(web, "LOGIN_GLOBAL_RATE_LIMIT_ATTEMPTS", None),
            EXPECTED_LOGIN_GLOBAL_RATE_LIMIT_ATTEMPTS,
        )
        self.setup_admin()
        token = self.client.csrf_for("/api/login")

        for _ in range(EXPECTED_LOGIN_IP_RATE_LIMIT_ATTEMPTS):
            self.assertEqual(
                self.client.post_json(
                    "/api/login",
                    {"username": "admin", "password": ADMIN_PASSWORD},
                    csrf=token,
                ).status,
                200,
            )

        limited = self.client.post_json(
            "/api/login",
            {"username": "admin", "password": ADMIN_PASSWORD},
            csrf=token,
        )
        self.assertEqual(limited.status, 429)
        self.assertLess(
            EXPECTED_LOGIN_IP_RATE_LIMIT_ATTEMPTS,
            EXPECTED_LOGIN_GLOBAL_RATE_LIMIT_ATTEMPTS,
        )

    def test_successful_login_clears_prior_failures_for_its_normalized_key(self):
        self.setup_admin()
        token = self.client.csrf_for("/api/login")
        for _ in range(4):
            self.assertEqual(
                self.client.post_json(
                    "/api/login", {"username": " ADMIN ", "password": "wrong-password"},
                    csrf=token,
                ).status,
                401,
            )
        self.assertEqual(
            self.client.post_json(
                "/api/login", {"username": "admin", "password": ADMIN_PASSWORD}, csrf=token
            ).status,
            200,
        )
        for _ in range(5):
            self.assertEqual(
                self.client.post_json(
                    "/api/login", {"username": "Admin", "password": "wrong-password"},
                    csrf=token,
                ).status,
                401,
            )
        self.assertEqual(
            self.client.post_json(
                "/api/login", {"username": "admin", "password": "wrong-password"},
                csrf=token,
            ).status,
            429,
        )

    def test_session_json_has_profile_and_csrf_without_security_versions(self):
        self.setup_admin()
        self.client.post_json("/api/login", {"username": "admin", "password": ADMIN_PASSWORD})
        response = self.client.get("/api/session")
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertEqual(
            set(payload["user"]),
            {"id", "username", "real_name", "department", "role", "must_change_password"},
        )
        self.assertTrue(payload["csrf_token"])
        serialized = response.body.lower()
        for forbidden in (b"password_version", b"status_version", b"password_hash", b"password_salt"):
            self.assertNotIn(forbidden, serialized)

    def test_forced_change_session_is_restricted(self):
        self.setup_admin()
        self.register()
        approved = self.approve_user()
        admin = self.running.users.authenticate("admin", ADMIN_PASSWORD)
        temporary_password = self.running.users.reset_password(admin.id, approved.id)
        self.assertEqual(self.client.post_json(
            "/api/login", {"username": "alice", "password": temporary_password}
        ).status, 200)
        self.assertEqual(self.client.get("/api/session").status, 200)
        self.assertEqual(self.client.get("/change-password").status, 200)
        root = self.client.get("/", follow_redirects=False)
        self.assertEqual((root.status, root.headers["Location"]), (303, "/change-password"))
        self.assertEqual(self.client.get("/api/admin/users").status, 403)

    def test_change_password_requires_csrf_clears_cookie_and_revokes_session(self):
        self.setup_admin()
        self.register()
        approved = self.approve_user()
        admin = self.running.users.authenticate("admin", ADMIN_PASSWORD)
        temporary_password = self.running.users.reset_password(admin.id, approved.id)
        self.client.post_json("/api/login", {"username": "alice", "password": temporary_password})

        denied = self.client.post_json(
            "/api/password/change",
            {"current_password": temporary_password, "new_password": "replacement password"},
            csrf=False,
        )
        self.assertEqual(denied.status, 403)
        response = self.client.post_json(
            "/api/password/change",
            {"current_password": temporary_password, "new_password": "replacement password"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["next"], "/login")
        self.assertIn("Max-Age=0", response.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/api/session").status, 401)

    def test_logout_requires_session_csrf_and_clears_cookie(self):
        self.setup_admin()
        self.client.post_json("/api/login", {"username": "admin", "password": ADMIN_PASSWORD})
        self.assertEqual(self.client.post_json("/api/logout", {}, csrf=False).status, 403)
        response = self.client.post_json("/api/logout", {})
        self.assertEqual(response.status, 200)
        self.assertIn("Max-Age=0", response.headers["Set-Cookie"])
        self.assertEqual(self.client.get("/api/session").status, 401)

    def test_html_redirects_follow_roles(self):
        self.setup_admin()
        admin_login = self.client.post_json(
            "/api/login", {"username": "admin", "password": ADMIN_PASSWORD}
        )
        self.assertEqual(admin_login.json()["next"], "/admin")
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["Location"], "/admin")
        self.assertEqual(self.client.get("/login", follow_redirects=False).headers["Location"], "/admin")

        registration_client = self.running.new_client()
        registration_client.post_json(
            "/api/register",
            {
                "username": "alice",
                "password": USER_PASSWORD,
                "real_name": "张三",
                "department": "技术部",
            },
        )
        self.approve_user()
        user_client = self.running.new_client()
        user_login = user_client.post_json(
            "/api/login", {"username": "alice", "password": USER_PASSWORD}
        )
        self.assertEqual(user_login.json()["next"], "/")
        self.assertEqual(user_client.get("/").status, 200)
        self.assertEqual(user_client.get("/login", follow_redirects=False).headers["Location"], "/")

    def test_json_parsing_rejects_size_content_type_syntax_and_non_objects(self):
        self.setup_admin()
        login_token = self.client.csrf_for("/api/login")
        common = [("X-CSRF-Token", login_token)]
        too_large = self.client.raw_request(
            "POST",
            "/api/login",
            headers=common + [
                ("Content-Type", "application/json"),
                ("Content-Length", str(self.running.max_body_bytes + 1)),
            ],
        )
        self.assertEqual(too_large.status, 413)
        for headers in (common + [("Content-Type", "application/json")],
                        common + [("Content-Type", "application/json"), ("Content-Length", "invalid")],
                        common + [("Content-Type", "application/json"), ("Content-Length", "-1")],
                        common + [("Content-Type", "application/json"), ("Content-Length", "9" * 5000)]):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.raw_request("POST", "/api/login", headers=headers).status, 400)
        ambiguous_lengths = (
            common + [("Content-Type", "application/json"), ("Content-Length", "+2")],
            common + [("Content-Type", "application/json"), ("Content-Length", "2"), ("Content-Length", "3")],
            common + [
                ("Content-Type", "application/json"),
                ("Content-Length", "2"),
                ("Transfer-Encoding", "chunked"),
            ],
        )
        for headers in ambiguous_lengths:
            with self.subTest(headers=headers):
                self.assertEqual(
                    self.client.raw_request("POST", "/api/login", body=b"{}", headers=headers).status,
                    400,
                )

        malformed = self.client.request(
            "POST", "/api/login", body=b"{bad", headers={"Content-Type": "application/json", "X-CSRF-Token": login_token}
        )
        non_object = self.client.post_json("/api/login", ["admin", ADMIN_PASSWORD], csrf=login_token)
        wrong_type = self.client.request(
            "POST", "/api/login", body=b"{}", headers={"Content-Type": "text/plain", "X-CSRF-Token": login_token}
        )
        self.assertEqual(malformed.status, 400)
        self.assertEqual(non_object.status, 400)
        self.assertEqual(wrong_type.status, 415)
        self.assertNotIn(b"traceback", malformed.body.lower())

        self.client.post_json("/api/login", {"username": "admin", "password": ADMIN_PASSWORD})
        session_csrf = self.client.get("/api/session").json()["csrf_token"]
        oversized_logout = self.client.raw_request(
            "POST",
            "/api/logout",
            headers=[
                ("Content-Type", "application/json"),
                ("Content-Length", str(self.running.max_body_bytes + 1)),
                ("X-CSRF-Token", session_csrf),
            ],
        )
        self.assertEqual(oversized_logout.status, 413)

    def test_static_traversal_encoded_traversal_and_symlink_escape_are_rejected(self):
        secret = self.running.root / "secret.js"
        secret.write_text("secret", encoding="utf-8")
        (self.running.static_dir / "escape.js").symlink_to(secret)
        for path in (
            "/static/../secret.js",
            "/static/%2e%2e/secret.js",
            "/static/%252e%252e/secret.js",
            "/static/escape.js",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status, 404)

    def test_method_unknown_route_and_security_headers(self):
        self.assertEqual(self.client.get("/api/setup").status, 405)
        self.assertEqual(self.client.request("POST", "/healthz", body=b"").status, 405)
        unsupported = self.client.request("PUT", "/login", body=b"")
        self.assertEqual(unsupported.status, 405)
        self.assertEqual(unsupported.headers["Content-Type"], "application/json; charset=utf-8")
        trace = self.client.request("TRACE", "/login")
        self.assertEqual(trace.status, 405)
        self.assertEqual(trace.headers["Content-Type"], "application/json; charset=utf-8")
        head = self.client.request("HEAD", "/login")
        self.assertEqual(head.status, 405)
        self.assertEqual(head.body, b"")
        self.assertEqual(self.client.get("/does-not-exist").status, 404)
        self.setup_admin()
        login = self.client.get("/login")
        self.assertEqual(login.headers["Cache-Control"], "no-store")
        self.assertEqual(login.headers["X-Content-Type-Options"], "nosniff")
        self.client.post_json("/api/login", {"username": "admin", "password": ADMIN_PASSWORD})
        session = self.client.get("/api/session")
        self.assertEqual(session.headers["Cache-Control"], "no-store")
        self.assertEqual(session.headers["X-Content-Type-Options"], "nosniff")

    def test_guard_failures_return_stable_json_without_internal_details(self):
        with self.assertLogs("web", level="ERROR"):
            with mock.patch.object(
                self.running.server.application.user_service,
                "setup_complete",
                side_effect=RuntimeError("private database detail"),
            ):
                response = self.client.get("/login")
        self.assertEqual(response.status, 500)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertNotIn(b"private database detail", response.body)

    def test_response_timeout_does_not_attempt_a_second_error_response(self):
        application = self.running.server.application
        handler = mock.Mock(command="GET")
        with mock.patch.object(application, "_json") as render_json:
            application._safely(handler, mock.Mock(side_effect=TimeoutError))
        render_json.assert_not_called()

    def test_callback_disconnects_close_without_a_second_response(self):
        application = self.running.server.application
        for error in (TimeoutError(), ConnectionError(), BrokenPipeError()):
            with self.subTest(error=type(error).__name__):
                handler = mock.Mock(
                    path="/healthz", headers={}, command="GET", close_connection=False
                )
                with mock.patch.object(
                    application, "_health", side_effect=error
                ), mock.patch.object(application, "_json") as render_json, mock.patch(
                    "web._LOGGER.exception"
                ) as log_exception:
                    application._dispatch(handler, "GET")
                render_json.assert_not_called()
                log_exception.assert_not_called()
                self.assertTrue(handler.close_connection)

    def test_callback_runtime_error_returns_safe_500(self):
        application = self.running.server.application
        handler = mock.Mock(path="/healthz", headers={}, command="GET")
        with mock.patch.object(
            application, "_health", side_effect=RuntimeError("private callback detail")
        ), mock.patch.object(application, "_json") as render_json, mock.patch(
            "web._LOGGER.exception"
        ) as log_exception:
            application._dispatch(handler, "GET")
        log_exception.assert_called_once()
        render_json.assert_called_once_with(
            handler, 500, {"error": "服务暂时不可用，请稍后重试"}
        )

    def test_legacy_anonymous_generate_and_filename_download_routes_are_absent(self):
        self.setup_admin()
        anonymous = self.running.new_client()
        self.assertEqual(anonymous.request("POST", "/generate", body=b"").status, 404)
        self.assertEqual(anonymous.get("/download/report.pdf").status, 404)


if __name__ == "__main__":
    unittest.main()
