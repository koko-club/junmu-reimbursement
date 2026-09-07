from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from security import AnonymousCsrfSigner, PasswordHasher, PasswordMaterial, load_or_create_secret


class PasswordHasherTest(unittest.TestCase):
    def test_hashing_same_password_uses_distinct_random_salts(self):
        hasher = PasswordHasher()

        first = hasher.hash("correct horse battery staple")
        second = hasher.hash("correct horse battery staple")

        self.assertIsInstance(first, PasswordMaterial)
        self.assertNotEqual(first.salt, second.salt)
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(len(first.salt), 16)
        self.assertEqual(len(first.digest), 32)

    def test_material_is_immutable(self):
        material = PasswordHasher().hash("correct horse battery staple")

        with self.assertRaises(FrozenInstanceError):
            material.salt = b"replacement"

    def test_verify_accepts_correct_password_and_rejects_wrong_password(self):
        hasher = PasswordHasher()
        material = hasher.hash("correct horse battery staple")

        self.assertTrue(hasher.verify("correct horse battery staple", material))
        self.assertFalse(hasher.verify("wrong horse battery staple", material))

    def test_hash_rejects_non_strings_and_out_of_range_unicode_lengths(self):
        hasher = PasswordHasher()

        for value in (None, b"password", 1234, "a" * 7, "a" * 129):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    hasher.hash(value)

        self.assertTrue(hasher.verify("a" * 8, hasher.hash("a" * 8)))
        self.assertTrue(hasher.verify("a" * 128, hasher.hash("a" * 128)))

    def test_verify_uses_the_parameters_stored_with_material(self):
        lower_cost_hasher = PasswordHasher(n=1024, r=8, p=1)
        material = lower_cost_hasher.hash("correct horse battery staple")

        self.assertTrue(PasswordHasher().verify("correct horse battery staple", material))

    def test_scrypt_allows_budgeted_work_factor_with_explicit_memory_limit(self):
        hasher = PasswordHasher(n=32768, r=8, p=1)

        material = hasher.hash("correct horse battery staple")

        self.assertTrue(hasher.verify("correct horse battery staple", material))

    def test_scrypt_rejects_parameters_over_memory_budget_in_hash_and_verify(self):
        with self.assertRaises(ValueError):
            PasswordHasher(n=65536, r=9, p=1)

        material = PasswordMaterial(
            digest=b"d" * 32,
            salt=b"s" * 16,
            params='{"n":65536,"r":9,"p":1}',
        )
        self.assertFalse(PasswordHasher().verify("correct horse battery staple", material))


class SecretFileTest(unittest.TestCase):
    def test_secret_is_reused_and_is_private_to_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "secret.bin"

            first = load_or_create_secret(path)
            second = load_or_create_secret(path)

            self.assertEqual(first, second)
            self.assertEqual(len(first), 32)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_concurrent_creators_share_one_complete_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "secret.bin"

            with ThreadPoolExecutor(max_workers=8) as executor:
                secrets = list(executor.map(load_or_create_secret, [path] * 8))

            self.assertEqual(len(set(secrets)), 1)
            self.assertEqual(len(secrets[0]), 32)

    def test_entropy_failure_removes_created_file_and_closes_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "secret.bin"

            with mock.patch("security.secrets.token_bytes", side_effect=OSError("no entropy")), \
                 mock.patch("security.os.close", wraps=os.close) as close:
                with self.assertRaisesRegex(OSError, "no entropy"):
                    load_or_create_secret(path)

            self.assertFalse(path.exists())
            close.assert_called_once()

    def test_second_creator_does_not_read_first_secret_before_it_is_committed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "secret.bin"
            first_fsync_started = threading.Event()
            release_first_fsync = threading.Event()
            fsync_calls = 0
            fsync_lock = threading.Lock()
            original_fsync = os.fsync
            generated_secrets = iter((b"a" * 32, b"b" * 32))

            def pause_first_fsync(descriptor):
                nonlocal fsync_calls
                with fsync_lock:
                    fsync_calls += 1
                    call_number = fsync_calls
                if call_number == 1:
                    first_fsync_started.set()
                    self.assertTrue(release_first_fsync.wait(timeout=2))
                return original_fsync(descriptor)

            with mock.patch("security.secrets.token_bytes", side_effect=lambda _: next(generated_secrets)), \
                 mock.patch("security.os.fsync", side_effect=pause_first_fsync):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first = executor.submit(load_or_create_secret, path)
                    self.assertTrue(first_fsync_started.wait(timeout=2))
                    second = executor.submit(load_or_create_secret, path)
                    self.assertEqual(second.result(timeout=2), b"b" * 32)
                    self.assertFalse(first.done())
                    release_first_fsync.set()
                    self.assertEqual(first.result(timeout=2), b"b" * 32)

    def test_directory_fsync_failure_preserves_published_secret_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "secret.bin"
            generated_secrets = iter((b"a" * 32, b"b" * 32))

            with mock.patch("security.secrets.token_bytes", side_effect=lambda _: next(generated_secrets)) as token_bytes:
                with mock.patch("security._fsync_directory", side_effect=OSError("directory sync failed")):
                    with self.assertRaisesRegex(OSError, "directory sync failed"):
                        load_or_create_secret(path)

                self.assertEqual(path.read_bytes(), b"a" * 32)
                self.assertEqual(load_or_create_secret(path), b"a" * 32)

            token_bytes.assert_called_once_with(32)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not supported on this platform")
    def test_existing_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "secret.fifo"
            os.mkfifo(path, 0o600)
            finished = threading.Event()
            captured = []

            def read_fifo():
                try:
                    load_or_create_secret(path)
                except BaseException as error:
                    captured.append(error)
                finally:
                    finished.set()

            thread = threading.Thread(target=read_fifo, daemon=True)
            thread.start()
            self.assertTrue(finished.wait(timeout=0.5))
            self.assertIsInstance(captured[0], ValueError)


class AnonymousCsrfSignerTest(unittest.TestCase):
    def setUp(self):
        self.signer = AnonymousCsrfSigner(b"s" * 32, ttl=1800)
        self.issued_at = 1_700_000_000
        self.token = self.signer.issue("post", "/setup?next=/dashboard", self.issued_at)

    def test_valid_token_verifies(self):
        self.assertTrue(self.signer.verify(
            self.token, "POST", "/setup?next=/dashboard", self.issued_at + 20
        ))

    def test_token_is_bound_to_original_path_and_normalized_method(self):
        self.assertFalse(self.signer.verify(
            self.token, "POST", "/setup?next=/other", self.issued_at + 20
        ))
        self.assertFalse(self.signer.verify(
            self.token, "GET", "/setup?next=/dashboard", self.issued_at + 20
        ))
        self.assertTrue(self.signer.verify(
            self.token, "post", "/setup?next=/dashboard", self.issued_at + 20
        ))

    def test_token_rejects_expired_and_future_timestamps(self):
        self.assertFalse(self.signer.verify(
            self.token, "POST", "/setup?next=/dashboard", self.issued_at + 1801
        ))
        self.assertFalse(self.signer.verify(
            self.token, "POST", "/setup?next=/dashboard", self.issued_at - 1
        ))

    def test_token_rejects_malformed_and_tampered_values(self):
        for token in ("", "not-a-token", "1.2", "1.2.3.4", self.token[:-1] + "x", None):
            with self.subTest(token=token):
                self.assertFalse(self.signer.verify(
                    token, "POST", "/setup?next=/dashboard", self.issued_at + 20
                ))

    def test_token_rejects_tampered_hex_signature(self):
        prefix, signature = self.token.rsplit(".", 1)
        replacement = "0" if signature[-1] != "0" else "1"
        tampered_token = f"{prefix}.{signature[:-1]}{replacement}"

        self.assertFalse(self.signer.verify(
            tampered_token, "POST", "/setup?next=/dashboard", self.issued_at + 20
        ))


if __name__ == "__main__":
    unittest.main()
