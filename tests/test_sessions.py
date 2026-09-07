from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import sqlite3
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest import mock


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from database import Database
from security import PasswordHasher
from sessions import AuthenticatedUser, IssuedSession, SessionService
from users import UserService


NOW = datetime(2026, 9, 7, 8, 30, tzinfo=timezone.utc)


class SessionServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "app.sqlite3")
        self.database.migrate()
        self.users = UserService(self.database, PasswordHasher(n=1024, r=8, p=1))
        self.admin = self.users.setup_admin(
            "admin", "correct horse battery staple", "Administrator", "Finance"
        )
        self.user = self._register_and_approve("alex")
        self.sessions = SessionService(self.database, clock=lambda: NOW)

    def tearDown(self):
        self.temporary.cleanup()

    def _register_and_approve(self, username):
        pending = self.users.register(
            username, "correct horse battery staple", username.title(), "Engineering"
        )
        return self.users.approve(self.admin.id, pending.id)

    def test_issue_returns_immutable_session_with_seven_day_utc_expiry(self):
        issued = self.sessions.issue(self.user)

        self.assertIsInstance(issued, IssuedSession)
        self.assertEqual(issued.expires_at, NOW + timedelta(days=7))
        self.assertEqual(issued.expires_at.tzinfo, timezone.utc)
        with self.assertRaises(AttributeError):
            issued.token = "replacement"

    def test_issue_rejects_user_id_without_an_authentication_snapshot(self):
        with self.assertRaises(ValueError):
            self.sessions.issue(self.user.id)

    def test_issue_replaces_the_prior_session_and_never_stores_plaintext_token(self):
        with mock.patch("sessions.secrets.token_urlsafe", side_effect=["first", "first-csrf", "second", "second-csrf"]):
            first = self.sessions.issue(self.user)
            second = self.sessions.issue(self.user)

        self.assertIsNone(self.sessions.resolve(first.token))
        self.assertEqual(self.sessions.resolve(second.token).user_id, self.user.id)
        with self.database.transaction() as connection:
            rows = connection.execute("SELECT token_hash, csrf_token FROM sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(bytes(rows[0]["token_hash"]), hashlib.sha256(b"second").digest())
        self.assertNotIn(b"second", bytes(rows[0]["token_hash"]))
        self.assertNotEqual(rows[0]["csrf_token"], second.token)

    def test_issue_rejects_non_active_accounts(self):
        pending = self.users.register(
            "pending", "correct horse battery staple", "Pending", "Engineering"
        )
        self.users.set_enabled(self.admin.id, self.user.id, False)

        for authenticated_user in (pending, self.user, 999999, None, True):
            with self.subTest(authenticated_user=authenticated_user):
                with self.assertRaises(ValueError):
                    self.sessions.issue(authenticated_user)

    def test_resolve_authenticates_both_roles_and_returns_user_profile(self):
        admin_session = self.sessions.issue(self.admin)
        user_session = self.sessions.issue(self.user)

        admin = self.sessions.resolve(admin_session.token)
        user = self.sessions.resolve(user_session.token)

        self.assertEqual(
            admin,
            AuthenticatedUser(
                user_id=self.admin.id, username="admin", real_name="Administrator",
                department="Finance", role="admin", must_change_password=False,
                csrf_token=admin_session.csrf_token,
            ),
        )
        self.assertEqual(user.user_id, self.user.id)
        self.assertEqual(user.role, "user")
        self.assertEqual(user.csrf_token, user_session.csrf_token)
        with self.assertRaises(AttributeError):
            user.username = "other"

    def test_resolve_rejects_invalid_tokens_and_disabled_accounts(self):
        issued = self.sessions.issue(self.user)
        self.users.set_enabled(self.admin.id, self.user.id, False)

        for token in (None, b"bytes", 7, "", "unknown", issued.token):
            with self.subTest(token=token):
                self.assertIsNone(self.sessions.resolve(token))
        with self.database.transaction() as connection:
            self.assertIsNone(connection.execute("SELECT * FROM sessions WHERE user_id = ?", (self.user.id,)).fetchone())

    def test_resolve_expires_at_the_exact_seven_day_boundary_and_removes_row(self):
        issued = self.sessions.issue(self.user)

        self.assertIsNotNone(self.sessions.resolve(issued.token, NOW + timedelta(days=7) - timedelta(microseconds=1)))
        self.assertIsNone(self.sessions.resolve(issued.token, NOW + timedelta(days=7)))
        with self.database.transaction() as connection:
            self.assertIsNone(connection.execute("SELECT * FROM sessions WHERE user_id = ?", (self.user.id,)).fetchone())

    def test_resolve_does_not_slide_session_expiration(self):
        issued = self.sessions.issue(self.user)
        self.sessions.resolve(issued.token, NOW + timedelta(days=2))

        with self.database.transaction() as connection:
            expires_at = connection.execute(
                "SELECT expires_at FROM sessions WHERE user_id = ?", (self.user.id,)
            ).fetchone()["expires_at"]
        self.assertEqual(expires_at, (NOW + timedelta(days=7)).isoformat())

    def test_verify_csrf_only_accepts_nonempty_matching_strings(self):
        issued = self.sessions.issue(self.user)
        authenticated = self.sessions.resolve(issued.token)

        self.assertTrue(self.sessions.verify_csrf(authenticated, issued.csrf_token))
        for submitted in ("wrong", "", None, b"bytes", 3):
            with self.subTest(submitted=submitted):
                self.assertFalse(self.sessions.verify_csrf(authenticated, submitted))
        self.assertFalse(self.sessions.verify_csrf(None, issued.csrf_token))

    def test_verify_csrf_rejects_non_ascii_submission_without_raising(self):
        issued = self.sessions.issue(self.user)
        authenticated = self.sessions.resolve(issued.token)

        self.assertFalse(self.sessions.verify_csrf(authenticated, "中文"))

    def test_revoke_token_and_user_are_idempotent(self):
        issued = self.sessions.issue(self.user)

        self.sessions.revoke_token(issued.token)
        self.sessions.revoke_token(issued.token)
        self.sessions.revoke_token(None)
        self.assertIsNone(self.sessions.resolve(issued.token))
        self.sessions.issue(self.user)
        self.sessions.revoke_user(self.user.id)
        self.sessions.revoke_user(self.user.id)
        self.sessions.revoke_user(None)
        with self.database.transaction() as connection:
            self.assertIsNone(connection.execute("SELECT * FROM sessions WHERE user_id = ?", (self.user.id,)).fetchone())

    def test_purge_expired_removes_only_sessions_at_or_past_utc_boundary(self):
        first = self.sessions.issue(self.user, NOW - timedelta(days=7))
        second_user = self._register_and_approve("sam")
        second = self.sessions.issue(second_user, NOW - timedelta(days=6, seconds=1))

        self.assertEqual(self.sessions.purge_expired(NOW), 1)
        self.assertIsNone(self.sessions.resolve(first.token, NOW))
        self.assertIsNotNone(self.sessions.resolve(second.token, NOW))

    def test_concurrent_issue_leaves_exactly_one_resolvable_session(self):
        def issue(_):
            return self.sessions.issue(self.user)

        with ThreadPoolExecutor(max_workers=4) as executor:
            issued = list(executor.map(issue, range(8)))

        resolved = [session for session in issued if self.sessions.resolve(session.token) is not None]
        self.assertEqual(len(resolved), 1)
        with self.database.transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (self.user.id,)).fetchone()[0], 1)

    def test_database_errors_close_transaction_connections(self):
        connection = mock.Mock()
        connection.execute.side_effect = [None, sqlite3.OperationalError("broken")]
        with mock.patch.object(self.database, "connect", return_value=connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "broken"):
                self.sessions.issue(self.user)
        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()


class UserServiceSessionIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "app.sqlite3")
        self.database.migrate()
        self.sessions = SessionService(self.database, clock=lambda: NOW)
        self.users = UserService(
            self.database, PasswordHasher(n=1024, r=8, p=1),
            revoke_sessions=self.sessions.revoke_user,
        )
        self.admin = self.users.setup_admin(
            "admin", "correct horse battery staple", "Administrator", "Finance"
        )
        pending = self.users.register(
            "alex", "correct horse battery staple", "Alex", "Engineering"
        )
        self.user = self.users.approve(self.admin.id, pending.id)

    def tearDown(self):
        self.temporary.cleanup()

    def test_disable_reset_and_change_password_revoke_persisted_session(self):
        issued = self.sessions.issue(self.user)
        self.users.set_enabled(self.admin.id, self.user.id, False)
        self.assertIsNone(self.sessions.resolve(issued.token))

        self.users.set_enabled(self.admin.id, self.user.id, True)
        issued = self.sessions.issue(
            self.users.authenticate("alex", "correct horse battery staple")
        )
        temporary = self.users.reset_password(self.admin.id, self.user.id)
        self.assertIsNone(self.sessions.resolve(issued.token))

        issued = self.sessions.issue(self.users.authenticate("alex", temporary))
        self.users.change_password(
            self.user.id, temporary, "a new correct horse battery staple"
        )
        self.assertIsNone(self.sessions.resolve(issued.token))

    def test_current_authentication_snapshot_can_issue_a_session(self):
        snapshot = self.users.authenticate(
            "alex", "correct horse battery staple"
        )

        issued = self.sessions.issue_authenticated(snapshot)

        self.assertEqual(self.sessions.resolve(issued.token).user_id, self.user.id)

    def test_stale_authentication_cannot_issue_after_password_reset(self):
        def reset_password():
            self.users.reset_password(self.admin.id, self.user.id)

        self._assert_stale_authentication_cannot_issue(reset_password)

    def test_stale_authentication_cannot_issue_after_disable_and_reenable(self):
        def disable_and_reenable():
            self.users.set_enabled(self.admin.id, self.user.id, False)
            self.users.set_enabled(self.admin.id, self.user.id, True)

        self._assert_stale_authentication_cannot_issue(disable_and_reenable)

    def _assert_stale_authentication_cannot_issue(self, mutate):
        authenticated = Event()
        mutation_complete = Event()
        result = []

        def stale_login():
            snapshot = self.users.authenticate(
                "alex", "correct horse battery staple"
            )
            authenticated.set()
            self.assertTrue(mutation_complete.wait(timeout=2))
            try:
                result.append(self.sessions.issue_authenticated(snapshot))
            except BaseException as error:
                result.append(error)

        worker = Thread(target=stale_login)
        worker.start()
        self.assertTrue(authenticated.wait(timeout=2))
        mutate()
        mutation_complete.set()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(result[0], ValueError)


if __name__ == "__main__":
    unittest.main()
