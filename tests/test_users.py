from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from database import Database
from security import PasswordHasher
from users import (
    AuthenticationFailed,
    InvalidState,
    PermissionDenied,
    SetupClosed,
    SetupRequired,
    UserNotFound,
    UserService,
    UsernameTaken,
    ValidationError,
)


class UserServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "app.sqlite3")
        self.database.migrate()
        self.revocations = []
        self.service = UserService(
            self.database,
            PasswordHasher(n=1024, r=8, p=1),
            revoke_sessions=self.revocations.append,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def setup_admin(self, username="admin"):
        return self.service.setup_admin(username, "correct horse battery staple", "Administrator", "Finance")

    def register_and_approve(self, username="alex"):
        admin = self.setup_admin()
        pending = self.service.register(username, "correct horse battery staple", "Alex", "Engineering")
        return admin, self.service.approve(admin.id, pending.id)

    def test_setup_is_atomic_concurrent_and_permanently_closed(self):
        other = UserService(self.database, PasswordHasher(n=1024, r=8, p=1))
        with ThreadPoolExecutor(max_workers=2) as executor:
            attempts = list(executor.map(
                lambda service: self._capture_setup(service), (self.service, other)
            ))

        successful = [result for result in attempts if not isinstance(result, BaseException)]
        self.assertEqual(len(successful), 1)
        self.assertTrue(self.service.setup_complete())
        self.assertEqual(successful[0].role, "admin")
        with self.assertRaises(SetupClosed):
            self.setup_admin("another-admin")

    def test_registration_requires_completed_setup(self):
        self.assertFalse(self.service.setup_complete())
        with self.assertRaises(SetupRequired):
            self.service.register("alex", "correct horse battery staple", "Alex", "Engineering")

    def test_setup_admin_is_active_without_an_approval_timestamp(self):
        admin = self.setup_admin()

        self.assertEqual(admin.status, "active")
        self.assertIsNone(admin.approved_at)

    def test_usernames_are_trimmed_nfkc_casefolded_and_unique(self):
        self.setup_admin()
        user = self.service.register("  A\uff2c\uff25X  ", "correct horse battery staple", "Alex", "Engineering")

        self.assertEqual(user.username, "A\uff2c\uff25X")
        with self.assertRaises(UsernameTaken):
            self.service.register("alex", "correct horse battery staple", "Other", "Engineering")
        for username in ("ab", "x" * 51, "   ", None):
            with self.subTest(username=username):
                with self.assertRaises(ValidationError):
                    self.service.register(username, "correct horse battery staple", "Alex", "Engineering")

    def test_profile_fields_are_required_bounded_and_formula_safe(self):
        self.setup_admin()
        for name, department in (("", "Engineering"), ("Alex", "\t"), ("=formula", "Engineering"), ("Alex", "+formula"), ("x" * 101, "Engineering")):
            with self.subTest(name=name, department=department):
                with self.assertRaises(ValidationError):
                    self.service.register("user" + str(len(name)), "correct horse battery staple", name, department)

    def test_pending_users_can_be_approved_or_rejected_and_rejection_releases_name(self):
        admin = self.setup_admin()
        pending = self.service.register("alex", "correct horse battery staple", " Alex ", " Engineering ")

        self.assertEqual([user.id for user in self.service.list_pending(admin.id)], [pending.id])
        approved = self.service.approve(admin.id, pending.id)
        self.assertEqual(approved.status, "active")
        self.assertIsNotNone(approved.approved_at)
        self.assertEqual(self.service.list_pending(admin.id), [])
        self.assertEqual([user.id for user in self.service.list_approved_users(admin.id)], [pending.id])

        rejected = self.service.register("reuse", "correct horse battery staple", "Reuse", "Engineering")
        self.service.reject(admin.id, rejected.id)
        replacement = self.service.register("reuse", "correct horse battery staple", "Reuse", "Engineering")
        self.assertEqual(replacement.username, "reuse")

    def test_admin_permission_and_target_state_matrix(self):
        admin = self.setup_admin()
        pending = self.service.register("alex", "correct horse battery staple", "Alex", "Engineering")
        with self.assertRaises(PermissionDenied):
            self.service.list_pending(pending.id)
        with self.assertRaises(PermissionDenied):
            self.service.approve(pending.id, pending.id)
        with self.assertRaises(InvalidState):
            self.service.set_enabled(admin.id, pending.id, False)
        with self.assertRaises(InvalidState):
            self.service.approve(admin.id, 99999)
        with self.assertRaises(InvalidState):
            self.service.reject(admin.id, 99999)
        with self.assertRaises(InvalidState):
            self.service.approve(admin.id, admin.id)
        with self.assertRaises(InvalidState):
            self.service.reject(admin.id, admin.id)
        with self.assertRaises(InvalidState):
            self.service.reject(admin.id, pending.id + 100)

        self.service.approve(admin.id, pending.id)
        with self.assertRaises(InvalidState):
            self.service.approve(admin.id, pending.id)
        with self.assertRaises(InvalidState):
            self.service.reject(admin.id, pending.id)
        with self.assertRaises(InvalidState):
            self.service.set_enabled(admin.id, admin.id, False)

    def test_profile_updates_only_approved_users_and_enable_transitions_revoke(self):
        admin, user = self.register_and_approve()
        updated = self.service.update_profile(admin.id, user.id, " Alex Updated ", " Sales ")
        self.assertEqual((updated.real_name, updated.department), ("Alex Updated", "Sales"))
        disabled = self.service.set_enabled(admin.id, user.id, False)
        self.assertEqual(disabled.status, "disabled")
        self.assertEqual(self.revocations, [user.id])
        active = self.service.set_enabled(admin.id, user.id, True)
        self.assertEqual(active.status, "active")
        self.assertEqual(self.revocations, [user.id])
        self.assertEqual([item.status for item in self.service.list_approved_users(admin.id)], ["active"])
        pending = self.service.register("pending", "correct horse battery staple", "Pending", "Engineering")
        with self.assertRaises(InvalidState):
            self.service.update_profile(admin.id, pending.id, "Name", "Department")
        with self.assertRaises(TypeError):
            self.service.update_profile(user.id, "Self update", "Engineering")

    def test_authentication_returns_only_active_users_with_no_password_material(self):
        admin, user = self.register_and_approve()
        authenticated = self.service.authenticate("  ALEX ", "correct horse battery staple")
        self.assertEqual(authenticated.id, user.id)
        self.assertFalse(hasattr(authenticated, "password_hash"))
        self.service.set_enabled(admin.id, user.id, False)
        for username, password in (("alex", "correct horse battery staple"), ("missing", "correct horse battery staple"), ("admin", "wrong password")):
            with self.subTest(username=username):
                with self.assertRaises(AuthenticationFailed):
                    self.service.authenticate(username, password)

    def test_pending_user_is_rejected_even_with_the_correct_password(self):
        self.setup_admin()
        self.service.register("pending", "correct horse battery staple", "Pending", "Engineering")

        with self.assertRaises(AuthenticationFailed):
            self.service.authenticate("pending", "correct horse battery staple")

    def test_reset_and_change_password_revoke_sessions_and_never_persist_plaintext(self):
        admin, user = self.register_and_approve()
        with mock.patch("users.secrets.token_urlsafe", return_value="temporary-secret"):
            temporary = self.service.reset_password(admin.id, user.id)
        self.assertEqual(temporary, "temporary-secret")
        self.assertEqual(self.revocations, [user.id])
        stored = self.service.get(user.id)
        self.assertTrue(stored.must_change_password)
        self.assertEqual(self.service.authenticate("alex", temporary).id, user.id)
        self.service.change_password(
            user.id,
            current_password=temporary,
            new_password="a new correct horse battery staple",
        )
        self.assertEqual(self.revocations, [user.id, user.id])
        self.assertFalse(self.service.get(user.id).must_change_password)
        with self.assertRaises(AuthenticationFailed):
            self.service.authenticate("alex", temporary)
        self.assertEqual(self.service.authenticate("alex", "a new correct horse battery staple").id, user.id)
        with self.database.transaction() as connection:
            saved = connection.execute("SELECT password_hash, password_salt, password_params FROM users WHERE id = ?", (user.id,)).fetchone()
        self.assertNotIn(temporary.encode(), tuple(saved))

    def test_password_reset_commits_before_a_database_backed_revoker_runs(self):
        admin, user = self.register_and_approve()
        observed = []

        def revoke(user_id):
            observed.append(self.service.get(user_id).must_change_password)
            with self.database.transaction(immediate=True) as connection:
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

        service = UserService(self.database, PasswordHasher(n=1024, r=8, p=1), revoke_sessions=revoke)
        service.reset_password(admin.id, user.id)

        self.assertEqual(observed, [True])

    def test_sensitive_actions_require_approved_user_and_roll_back_when_revocation_fails(self):
        admin, user = self.register_and_approve()
        pending = self.service.register("pending", "correct horse battery staple", "Pending", "Engineering")
        with self.assertRaises(InvalidState):
            self.service.reset_password(admin.id, pending.id)
        with self.assertRaises(AuthenticationFailed):
            self.service.change_password(user.id, "wrong password", "a new correct horse battery staple")

        failing = UserService(self.database, PasswordHasher(n=1024, r=8, p=1), revoke_sessions=lambda _: (_ for _ in ()).throw(RuntimeError("revoke failed")))
        with self.assertLogs("users", level="ERROR") as logged:
            with self.assertRaisesRegex(RuntimeError, "revoke failed"):
                failing.set_enabled(admin.id, user.id, False)
        self.assertIn("session revocation failed", logged.output[0])
        self.assertEqual(self.service.get(user.id).status, "active")
        with self.assertLogs("users", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "revoke failed"):
                failing.reset_password(admin.id, user.id)
        self.assertEqual(self.service.authenticate("alex", "correct horse battery staple").id, user.id)
        with self.assertLogs("users", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "revoke failed"):
                failing.change_password(
                    user.id,
                    "correct horse battery staple",
                    "a new correct horse battery staple",
                )
        self.assertEqual(self.service.authenticate("alex", "correct horse battery staple").id, user.id)

    def test_failed_revocation_restores_only_security_fields_after_concurrent_profile_edit(self):
        admin, user = self.register_and_approve()

        def change_profile_then_fail(user_id):
            self.service.update_profile(admin.id, user_id, "Concurrent Name", "Concurrent Department")
            raise RuntimeError("revoke failed")

        failing = UserService(
            self.database,
            PasswordHasher(n=1024, r=8, p=1),
            revoke_sessions=change_profile_then_fail,
        )
        with self.assertLogs("users", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "revoke failed"):
                failing.reset_password(admin.id, user.id)
        restored = self.service.get(user.id)
        self.assertEqual((restored.real_name, restored.department), ("Concurrent Name", "Concurrent Department"))
        self.assertFalse(restored.must_change_password)
        self.assertEqual(self.service.authenticate("alex", "correct horse battery staple").id, user.id)

        with self.assertLogs("users", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "revoke failed"):
                failing.set_enabled(admin.id, user.id, False)
        restored = self.service.get(user.id)
        self.assertEqual(restored.status, "active")
        self.assertEqual((restored.real_name, restored.department), ("Concurrent Name", "Concurrent Department"))

    def test_failed_revocation_does_not_overwrite_a_later_password_change(self):
        admin, user = self.register_and_approve()

        def replace_password_then_fail(user_id):
            material = PasswordHasher(n=1024, r=8, p=1).hash("concurrent correct horse battery staple")
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE users SET password_hash = ?, password_salt = ?, password_params = ?, "
                    "must_change_password = 1 WHERE id = ?",
                    (material.digest, material.salt, material.params, user_id),
                )
            raise RuntimeError("revoke failed")

        failing = UserService(
            self.database,
            PasswordHasher(n=1024, r=8, p=1),
            revoke_sessions=replace_password_then_fail,
        )
        with self.assertLogs("users", level="ERROR") as logged:
            with self.assertRaisesRegex(RuntimeError, "revoke failed"):
                failing.reset_password(admin.id, user.id)
        self.assertIn("could not restore password", "\n".join(logged.output))
        self.assertTrue(self.service.get(user.id).must_change_password)
        self.assertEqual(
            self.service.authenticate("alex", "concurrent correct horse battery staple").id,
            user.id,
        )

    def test_get_raises_for_unknown_user_and_lists_have_stable_order(self):
        admin = self.setup_admin()
        first = self.service.register("first", "correct horse battery staple", "First", "Engineering")
        second = self.service.register("second", "correct horse battery staple", "Second", "Engineering")
        self.assertEqual([user.id for user in self.service.list_pending(admin.id)], [first.id, second.id])
        with self.assertRaises(UserNotFound):
            self.service.get(99999)

    @staticmethod
    def _capture_setup(service):
        try:
            return service.setup_admin("admin", "correct horse battery staple", "Administrator", "Finance")
        except BaseException as error:
            return error


if __name__ == "__main__":
    unittest.main()
