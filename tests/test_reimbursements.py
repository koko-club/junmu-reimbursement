from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest import mock
from uuid import UUID

from openpyxl import load_workbook

import app
from config import AppConfig
from database import Database
from generator import GenerationResult, generate_workbook
from reimbursements import (
    ReimbursementGenerationError,
    ReimbursementNotFound,
    ReimbursementRecord,
    ReimbursementService,
)
from sessions import AuthenticatedUser


def valid_payload(**overrides) -> dict:
    value = {
        "date": "2026-09-08",
        "department": "客户端部门",
        "traveler": "客户端姓名",
        "reason": "客户拜访",
        "days": "2",
        "allowance": "50.5",
        "rows": [
            {
                "origin": "上海",
                "destination": "杭州",
                "mileage": "10",
                "receipts": "2",
            }
        ],
    }
    value.update(overrides)
    return value


def authenticated_user(user_id: int, name: str, department: str, role: str = "user"):
    return AuthenticatedUser(
        user_id=user_id,
        username=f"user-{user_id}",
        real_name=name,
        department=department,
        role=role,
        must_change_password=False,
        csrf_token="csrf-token",
    )


class ReimbursementServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "data"
        self.template = self.root / "template.xlsx"
        self.template.write_bytes(b"template")
        self.database = Database(self.data_dir / "database" / "app.db")
        self.database.migrate()
        self.alice = authenticated_user(1, "张三", "技术部")
        self.bob = authenticated_user(2, "李四", "财务部")
        self._insert_user(self.alice)
        self._insert_user(self.bob)
        self.generated_payloads = []
        self.generated_images = []

        def generate(template_path, work_dir, payload, image_paths):
            self.assertEqual(Path(template_path), self.template)
            self.generated_payloads.append(payload)
            self.generated_images.append(list(image_paths))
            path = Path(work_dir) / "相同显示名.xlsx"
            path.write_bytes(b"xlsx")
            return GenerationResult(path=path, screenshot_count=len(image_paths), receipt_count=2)

        def export(xlsx_path, work_dir, soffice_path):
            self.assertEqual(Path(xlsx_path).read_bytes(), b"xlsx")
            self.assertEqual(Path(soffice_path), Path("/fake/soffice"))
            path = Path(work_dir) / "相同显示名.pdf"
            path.write_bytes(b"pdf")
            return path

        self.generator = mock.Mock(side_effect=generate)
        self.exporter = mock.Mock(side_effect=export)
        self.config = AppConfig(
            template_path=self.template,
            data_dir=self.data_dir,
            templates_dir=self.root / "templates",
            static_dir=self.root / "static",
            host="127.0.0.1",
            port=8800,
            soffice_path="/fake/soffice",
            max_body_bytes=1024,
            max_concurrent_generations=2,
            cookie_secure=False,
        )
        self.service = ReimbursementService(
            self.database,
            self.config,
            workbook_generator=self.generator,
            pdf_exporter=self.exporter,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _insert_user(self, user: AuthenticatedUser) -> None:
        now = "2026-09-08T00:00:00+00:00"
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO users(
                    id, username, username_key, password_hash, password_salt,
                    password_params, real_name, department, role, status,
                    must_change_password, created_at, approved_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)""",
                (
                    user.user_id,
                    user.username,
                    user.username,
                    b"hash",
                    b"salt",
                    "{}",
                    user.real_name,
                    user.department,
                    user.role,
                    now,
                    now,
                    now,
                ),
            )

    def _assert_no_history_or_artifacts(self) -> None:
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM reimbursements").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(list((self.data_dir / "tmp").glob("*")), [])
        self.assertEqual(list((self.data_dir / "users").rglob("*.xlsx")), [])
        self.assertEqual(list((self.data_dir / "users").rglob("*.pdf")), [])

    def _service_with(self, **overrides) -> ReimbursementService:
        arguments = {
            "workbook_generator": self.generator,
            "pdf_exporter": self.exporter,
        }
        arguments.update(overrides)
        return ReimbursementService(self.database, self.config, **arguments)

    def _insert_record(
        self,
        user_id: int,
        record_id: str,
        *,
        created_at: str,
        deleted_at: str | None = None,
    ) -> tuple[Path, Path]:
        final_dir = self.data_dir.resolve() / "users" / str(user_id) / record_id
        final_dir.mkdir(parents=True)
        xlsx = final_dir / "history.xlsx"
        pdf = final_dir / "history.pdf"
        xlsx.write_bytes(b"xlsx-history")
        pdf.write_bytes(b"pdf-history")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO reimbursements(
                    id, user_id, reimbursement_date, display_name,
                    xlsx_path, pdf_path, created_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    user_id,
                    "2026-09-08",
                    "history.xlsx",
                    xlsx.relative_to(self.data_dir.resolve()).as_posix(),
                    pdf.relative_to(self.data_dir.resolve()).as_posix(),
                    created_at,
                    deleted_at,
                ),
            )
        return xlsx, pdf

    def test_generate_uses_trusted_profile_records_owner_and_moves_private_files(self):
        record = self.service.generate(
            self.alice,
            valid_payload(date="", department="=CLIENT", traveler=None),
            [],
        )

        payload = self.generated_payloads[0]
        self.assertEqual(payload["traveler"], self.alice.real_name)
        self.assertEqual(payload["department"], self.alice.department)
        self.assertEqual(payload["date"], "")
        self.assertEqual(len(payload["rows"]), 11)
        self.assertEqual(record.user_id, self.alice.user_id)
        self.assertEqual(UUID(record.id).version, 4)
        self.assertEqual(record.reimbursement_date, "")
        self.assertEqual(record.display_name, "相同显示名.xlsx")
        self.assertEqual(datetime.fromisoformat(record.created_at).utcoffset(), timedelta(0))
        final_dir = self.data_dir / "users" / str(self.alice.user_id) / record.id
        self.assertEqual(stat.S_IMODE(final_dir.stat().st_mode), 0o700)
        self.assertEqual((final_dir / "相同显示名.xlsx").read_bytes(), b"xlsx")
        self.assertEqual((final_dir / "相同显示名.pdf").read_bytes(), b"pdf")
        self.assertFalse((self.data_dir / "tmp" / record.id).exists())
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reimbursements WHERE id = ?", (record.id,)
            ).fetchone()
        self.assertEqual(row["user_id"], self.alice.user_id)
        self.assertEqual(row["xlsx_path"], f"users/{self.alice.user_id}/{record.id}/相同显示名.xlsx")
        self.assertEqual(row["pdf_path"], f"users/{self.alice.user_id}/{record.id}/相同显示名.pdf")

    def test_record_is_immutable(self):
        record = self.service.generate(self.alice, valid_payload(), [])
        self.assertIsInstance(record, ReimbursementRecord)
        with self.assertRaises(FrozenInstanceError):
            record.display_name = "changed.xlsx"

    def test_generate_rejects_admin_and_invalid_user_id_before_dependencies(self):
        cases = (
            authenticated_user(3, "管理员", "管理部", role="admin"),
            authenticated_user(0, "非法", "管理部"),
        )
        for user in cases:
            with self.subTest(user=user), self.assertRaises(ReimbursementGenerationError):
                self.service.generate(user, valid_payload(), [])
        self.generator.assert_not_called()
        self.exporter.assert_not_called()
        self.assertFalse((self.data_dir / "tmp").exists())

    def test_same_display_name_for_two_users_does_not_conflict(self):
        alice_record = self.service.generate(self.alice, valid_payload(), [])
        bob_record = self.service.generate(self.bob, valid_payload(), [])

        self.assertEqual(alice_record.display_name, bob_record.display_name)
        self.assertNotEqual(alice_record.id, bob_record.id)
        self.assertTrue((self.data_dir / alice_record.xlsx_path).is_file())
        self.assertTrue((self.data_dir / bob_record.xlsx_path).is_file())

    def test_display_name_rejects_unsafe_metadata_values(self):
        unsafe_names = (
            "",
            ".",
            "..",
            "folder/name.xlsx",
            "folder\\name.xlsx",
            "missing-extension",
            "\ud800.xlsx",
            "a" * 176 + ".xlsx",
            *(f"control-{chr(code)}.xlsx" for code in range(32)),
            "control-\x7f.xlsx",
        )

        for name in unsafe_names:
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                ReimbursementService._validated_display_name(name)

    def test_display_name_accepts_legal_unicode_xlsx_name(self):
        name = "2026年上海差旅报销单.xlsx"

        self.assertEqual(ReimbursementService._validated_display_name(name), name)

    def test_dangerous_display_name_is_not_persisted_or_logged(self):
        dangerous_name = "private-token\r\nInjected-Header.xlsx"

        def dangerous_generator(_template, work_dir, _payload, _images):
            path = Path(work_dir) / dangerous_name
            path.write_bytes(b"xlsx")
            return GenerationResult(path, 0, 0)

        service = self._service_with(workbook_generator=dangerous_generator)

        with self.assertLogs("reimbursements", level="ERROR") as captured:
            with self.assertRaisesRegex(
                ReimbursementGenerationError, "^生成报销文件失败，请稍后重试$"
            ):
                service.generate(self.alice, valid_payload(), [])

        self.assertNotIn(dangerous_name, "\n".join(captured.output))
        self._assert_no_history_or_artifacts()

    def test_dangerous_pdf_name_is_not_persisted_or_logged(self):
        dangerous_name = "private-pdf-token\r\nInjected-Header.pdf"

        def dangerous_exporter(_xlsx, work_dir, _soffice):
            path = Path(work_dir) / dangerous_name
            path.write_bytes(b"pdf")
            return path

        service = self._service_with(pdf_exporter=dangerous_exporter)

        with self.assertLogs("reimbursements", level="ERROR") as captured:
            with self.assertRaisesRegex(
                ReimbursementGenerationError, "^生成报销文件失败，请稍后重试$"
            ):
                service.generate(self.alice, valid_payload(), [])

        combined = "\n".join(captured.output)
        self.assertNotIn("private-pdf-token", combined)
        self.assertNotIn("Injected-Header", combined)
        self._assert_no_history_or_artifacts()

    def test_long_unicode_profiles_generate_bounded_private_files(self):
        template = Path(__file__).resolve().parents[1] / "resources" / "差旅报销单模板.xlsx"
        observed_payloads = []

        def real_generator(template_path, work_dir, payload, image_paths):
            observed_payloads.append(payload)
            return generate_workbook(template_path, work_dir, payload, image_paths)

        def successful_export(_xlsx, work_dir, _soffice):
            pdf = Path(work_dir) / "converted.pdf"
            pdf.write_bytes(b"pdf")
            return pdf

        service = ReimbursementService(
            self.database,
            replace(self.config, template_path=template),
            workbook_generator=real_generator,
            pdf_exporter=successful_export,
        )

        for length in (50, 100):
            traveler = "张" * length
            user = replace(self.alice, real_name=traveler)
            with self.subTest(length=length):
                record = service.generate(user, valid_payload(), [])
                self.assertLessEqual(len(record.display_name.encode("utf-8")), 180)
                self.assertTrue(record.display_name.endswith("-差旅报销单.xlsx"))
                self.assertLessEqual(
                    len(observed_payloads[-1]["output_stem"].encode("utf-8")),
                    150,
                )
                with service.owned_file(user.user_id, record.id, "xlsx") as owned:
                    workbook = load_workbook(owned.stream, data_only=False)
                    try:
                        self.assertEqual(
                            workbook["差旅报销单"]["B5"].value,
                            traveler,
                        )
                    finally:
                        workbook.close()
                with service.owned_file(user.user_id, record.id, "pdf") as owned:
                    self.assertEqual(owned.stream.read(), b"pdf")

    def test_pdf_failure_leaves_no_files_or_history(self):
        self.exporter.side_effect = RuntimeError("conversion failed with private path")

        with self.assertRaisesRegex(
            ReimbursementGenerationError, "^生成报销文件失败，请稍后重试$"
        ):
            self.service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_generator_external_path_is_rejected_without_deleting_external_file(self):
        external = self.root / "external.xlsx"
        external.write_bytes(b"external-xlsx")
        self.generator.side_effect = lambda *_args: GenerationResult(external, 0, 0)

        with self.assertRaises(ReimbursementGenerationError):
            self.service.generate(self.alice, valid_payload(), [])

        self.assertEqual(external.read_bytes(), b"external-xlsx")
        self.exporter.assert_not_called()
        self._assert_no_history_or_artifacts()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not supported")
    def test_generator_fifo_is_rejected_without_blocking(self):
        record_uuid = UUID("22222222-2222-4222-8222-222222222224")
        fifo_ready = threading.Event()

        def fifo_generator(_template, work_dir, _payload, _images):
            path = Path(work_dir) / "blocked.xlsx"
            os.mkfifo(path, 0o600)
            fifo_ready.set()
            return GenerationResult(path, 0, 0)

        service = self._service_with(
            uuid_factory=lambda: record_uuid,
            workbook_generator=fifo_generator,
        )
        done = threading.Event()
        errors = []

        def generate():
            try:
                service.generate(self.alice, valid_payload(), [])
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        worker = threading.Thread(target=generate, daemon=True)
        worker.start()
        self.assertTrue(fifo_ready.wait(1))
        fifo_path = self.data_dir / "tmp" / str(record_uuid) / "blocked.xlsx"
        writer_fd = None
        try:
            completed_without_writer = done.wait(0.2)
            if not completed_without_writer:
                writer_fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
            self.assertTrue(done.wait(1), "generation blocked while opening a FIFO")
        finally:
            if writer_fd is not None:
                os.close(writer_fd)
            worker.join(1)

        self.assertTrue(completed_without_writer)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReimbursementGenerationError)
        self._assert_no_history_or_artifacts()

    def test_generator_symlink_is_rejected_without_deleting_target(self):
        target = self.root / "target.xlsx"
        target.write_bytes(b"external-target")

        def symlink_result(_template, work_dir, _payload, _images):
            link = Path(work_dir) / "linked.xlsx"
            link.symlink_to(target)
            return GenerationResult(link, 0, 0)

        self.generator.side_effect = symlink_result

        with self.assertRaises(ReimbursementGenerationError):
            self.service.generate(self.alice, valid_payload(), [])

        self.assertEqual(target.read_bytes(), b"external-target")
        self.exporter.assert_not_called()
        self._assert_no_history_or_artifacts()

    def test_generator_external_hardlink_is_rejected_without_deleting_target(self):
        external = self.root / "external-hardlink.xlsx"
        external.write_bytes(b"external-target")

        def hardlink_result(_template, work_dir, _payload, _images):
            linked = Path(work_dir) / "linked.xlsx"
            linked.hardlink_to(external)
            return GenerationResult(linked, 0, 0)

        self.generator.side_effect = hardlink_result

        with self.assertRaises(ReimbursementGenerationError):
            self.service.generate(self.alice, valid_payload(), [])

        self.assertEqual(external.read_bytes(), b"external-target")
        self.assertEqual(external.stat().st_nlink, 1)
        self.exporter.assert_not_called()
        self._assert_no_history_or_artifacts()

    def test_generator_path_with_parent_component_is_rejected(self):
        def parent_result(_template, work_dir, _payload, _images):
            work_dir = Path(work_dir)
            (work_dir / "inner").mkdir()
            (work_dir / "parent.xlsx").write_bytes(b"xlsx")
            path = work_dir / "inner" / ".." / "parent.xlsx"
            return GenerationResult(path, 0, 0)

        self.generator.side_effect = parent_result

        with self.assertRaises(ReimbursementGenerationError):
            self.service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_pdf_external_path_is_rejected_without_deleting_external_file(self):
        external = self.root / "external.pdf"
        external.write_bytes(b"external-pdf")
        self.exporter.side_effect = lambda *_args: external

        with self.assertRaises(ReimbursementGenerationError):
            self.service.generate(self.alice, valid_payload(), [])

        self.assertEqual(external.read_bytes(), b"external-pdf")
        self._assert_no_history_or_artifacts()

    def test_pdf_symlink_is_rejected_without_deleting_target(self):
        target = self.root / "target.pdf"
        target.write_bytes(b"external-target")

        def symlink_pdf(_xlsx, work_dir, _soffice):
            link = Path(work_dir) / "linked.pdf"
            link.symlink_to(target)
            return link

        self.exporter.side_effect = symlink_pdf

        with self.assertRaises(ReimbursementGenerationError):
            self.service.generate(self.alice, valid_payload(), [])

        self.assertEqual(target.read_bytes(), b"external-target")
        self._assert_no_history_or_artifacts()

    def test_pdf_external_hardlink_is_rejected_without_deleting_target(self):
        external = self.root / "external-hardlink.pdf"
        external.write_bytes(b"external-target")

        def hardlink_pdf(_xlsx, work_dir, _soffice):
            linked = Path(work_dir) / "linked.pdf"
            linked.hardlink_to(external)
            return linked

        self.exporter.side_effect = hardlink_pdf

        with self.assertRaises(ReimbursementGenerationError):
            self.service.generate(self.alice, valid_payload(), [])

        self.assertEqual(external.read_bytes(), b"external-target")
        self.assertEqual(external.stat().st_nlink, 1)
        self._assert_no_history_or_artifacts()

    def test_pdf_must_not_be_same_file_as_workbook(self):
        def hard_link_pdf(xlsx_path, work_dir, _soffice):
            pdf = Path(work_dir) / "same-content.pdf"
            pdf.hardlink_to(xlsx_path)
            return pdf

        self.exporter.side_effect = hard_link_pdf

        with self.assertRaises(ReimbursementGenerationError):
            self.service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_exporter_cannot_replace_workbook_with_symlink_to_external_file(self):
        external = self.root / "external-after-export.xlsx"
        external.write_bytes(b"external-target")

        def replace_with_symlink(xlsx_path, work_dir, _soffice):
            Path(xlsx_path).unlink()
            Path(xlsx_path).symlink_to(external)
            pdf = Path(work_dir) / "converted.pdf"
            pdf.write_bytes(b"pdf")
            return pdf

        self.exporter.side_effect = replace_with_symlink

        with self.assertRaisesRegex(
            ReimbursementGenerationError, "^生成报销文件失败，请稍后重试$"
        ):
            self.service.generate(self.alice, valid_payload(), [])

        self.assertEqual(external.read_bytes(), b"external-target")
        self._assert_no_history_or_artifacts()

    def test_exporter_cannot_delete_workbook_before_returning_pdf(self):
        def delete_workbook(xlsx_path, work_dir, _soffice):
            Path(xlsx_path).unlink()
            pdf = Path(work_dir) / "converted.pdf"
            pdf.write_bytes(b"pdf")
            return pdf

        self.exporter.side_effect = delete_workbook

        with self.assertRaisesRegex(
            ReimbursementGenerationError, "^生成报销文件失败，请稍后重试$"
        ):
            self.service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_exporter_cannot_replace_workbook_with_directory(self):
        def replace_with_directory(xlsx_path, work_dir, _soffice):
            Path(xlsx_path).unlink()
            Path(xlsx_path).mkdir()
            pdf = Path(work_dir) / "converted.pdf"
            pdf.write_bytes(b"pdf")
            return pdf

        self.exporter.side_effect = replace_with_directory

        with self.assertRaisesRegex(
            ReimbursementGenerationError, "^生成报销文件失败，请稍后重试$"
        ):
            self.service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_exporter_cannot_replace_workbook_with_different_regular_file(self):
        def replace_with_regular_file(xlsx_path, work_dir, _soffice):
            original = Path(work_dir) / "renamed-original.xlsx"
            Path(xlsx_path).replace(original)
            Path(xlsx_path).write_bytes(b"replacement-xlsx")
            pdf = Path(work_dir) / "converted.pdf"
            pdf.write_bytes(b"pdf")
            return pdf

        self.exporter.side_effect = replace_with_regular_file

        with self.assertRaisesRegex(
            ReimbursementGenerationError, "^生成报销文件失败，请稍后重试$"
        ):
            self.service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_screenshots_must_be_supported_existing_regular_files(self):
        target = self.root / "caller.png"
        target.write_bytes(b"caller-owned")
        linked = self.root / "linked.png"
        linked.symlink_to(target)
        cases = (self.root / "missing.png", self.root / "receipt.gif", linked)

        for screenshot in cases:
            with self.subTest(screenshot=screenshot), self.assertRaises(
                ReimbursementGenerationError
            ):
                self.service.generate(self.alice, valid_payload(), [screenshot])

        self.assertEqual(target.read_bytes(), b"caller-owned")
        self.generator.assert_not_called()
        self._assert_no_history_or_artifacts()

    def test_valid_screenshot_is_passed_through_and_not_deleted(self):
        screenshot = self.root / "caller.PNG"
        screenshot.write_bytes(b"caller-owned")

        self.service.generate(self.alice, valid_payload(), [screenshot])

        self.assertEqual(self.generated_images, [[screenshot]])
        self.assertEqual(screenshot.read_bytes(), b"caller-owned")

    def test_generation_semaphore_queues_requests_and_bounds_peak_execution(self):
        entered_first = threading.Event()
        entered_second = threading.Event()
        release = threading.Event()
        count_lock = threading.Lock()
        active = 0
        peak = 0

        def blocking_generator(_template, work_dir, _payload, _images):
            nonlocal active, peak
            with count_lock:
                active += 1
                peak = max(peak, active)
                (entered_first if active == 1 else entered_second).set()
            try:
                self.assertTrue(release.wait(2), "test did not release generator")
                path = Path(work_dir) / "queued.xlsx"
                path.write_bytes(b"xlsx")
                return GenerationResult(path, 0, 0)
            finally:
                with count_lock:
                    active -= 1

        service = ReimbursementService(
            self.database,
            replace(self.config, max_concurrent_generations=1),
            workbook_generator=blocking_generator,
            pdf_exporter=self.exporter,
        )
        records = []
        errors = []

        def run(user):
            try:
                records.append(service.generate(user, valid_payload(), []))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        first = threading.Thread(target=run, args=(self.alice,))
        second = threading.Thread(target=run, args=(self.bob,))
        first.start()
        self.assertTrue(entered_first.wait(1))
        second.start()
        try:
            second_was_queued = not entered_second.wait(0.15)
        finally:
            release.set()
            first.join(2)
            second.join(2)

        self.assertTrue(second_was_queued)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(peak, 1)

    def test_validation_failure_does_not_wait_for_generation_slot(self):
        generator_entered = threading.Event()
        release_generator = threading.Event()
        invalid_done = threading.Event()
        errors = []

        def blocking_generator(_template, work_dir, _payload, _images):
            generator_entered.set()
            self.assertTrue(release_generator.wait(2))
            path = Path(work_dir) / "blocked.xlsx"
            path.write_bytes(b"xlsx")
            return GenerationResult(path, 0, 0)

        service = ReimbursementService(
            self.database,
            replace(self.config, max_concurrent_generations=1),
            workbook_generator=blocking_generator,
            pdf_exporter=self.exporter,
        )

        def valid_request():
            try:
                service.generate(self.alice, valid_payload(), [])
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        def invalid_request():
            try:
                service.generate(self.bob, valid_payload(reason="=FORMULA"), [])
            except Exception as error:
                errors.append(error)
            finally:
                invalid_done.set()

        first = threading.Thread(target=valid_request)
        invalid = threading.Thread(target=invalid_request)
        first.start()
        self.assertTrue(generator_entered.wait(1))
        invalid.start()
        try:
            failed_without_waiting = invalid_done.wait(0.15)
        finally:
            release_generator.set()
            first.join(2)
            invalid.join(2)

        self.assertTrue(failed_without_waiting)
        self.assertFalse(first.is_alive())
        self.assertFalse(invalid.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReimbursementGenerationError)

    def test_move_failure_removes_only_this_requests_work_directory(self):
        screenshot = self.root / "caller.png"
        screenshot.write_bytes(b"caller")

        def fail_move(_source, _destination, **_kwargs):
            raise OSError("move failed at secret path")

        service = self._service_with(move_directory=fail_move)
        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [screenshot])

        self.assertEqual(self.template.read_bytes(), b"template")
        self.assertEqual(screenshot.read_bytes(), b"caller")
        self._assert_no_history_or_artifacts()

    def test_cleanup_does_not_follow_replaced_tmp_root_to_external_directory(self):
        external = self.root / "external-tmp-target"
        detached_tmp = self.root / "detached-tmp"
        sentinel = None
        detached_work = None

        def replace_tmp_then_fail(_xlsx, work_dir, _soffice):
            nonlocal sentinel, detached_work
            work_dir = Path(work_dir)
            tmp_root = work_dir.parent
            detached_work = detached_tmp / work_dir.name
            external_work = external / work_dir.name
            external_work.mkdir(parents=True)
            sentinel = external_work / "DO-NOT-DELETE"
            sentinel.write_bytes(b"external")
            tmp_root.rename(detached_tmp)
            tmp_root.symlink_to(external, target_is_directory=True)
            raise RuntimeError("forced export failure")

        service = self._service_with(pdf_exporter=replace_tmp_then_fail)

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self.assertIsNotNone(sentinel)
        self.assertEqual(sentinel.read_bytes(), b"external")
        self.assertFalse(detached_work.exists())
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reimbursements").fetchone()[0], 0)

    def test_cleanup_does_not_follow_replaced_users_root_to_external_directory(self):
        record_uuid = UUID("11111111-1111-4111-8111-111111111111")
        existing_id = "11111111-1111-4111-8111-111111111112"
        existing_xlsx, _ = self._insert_record(
            self.alice.user_id,
            existing_id,
            created_at="2026-09-07T00:00:00+00:00",
        )
        users_root = self.data_dir / "users"
        detached_users = self.root / "detached-users"
        external_users = self.root / "external-users-target"
        external_record = external_users / str(self.alice.user_id) / str(record_uuid)
        external_record.mkdir(parents=True)
        sentinel = external_record / "DO-NOT-DELETE"
        sentinel.write_bytes(b"external")

        def replace_users_then_fail(_source, _destination, **_kwargs):
            users_root.rename(detached_users)
            users_root.symlink_to(external_users, target_is_directory=True)
            raise OSError("forced move failure")

        service = self._service_with(
            uuid_factory=lambda: record_uuid,
            move_directory=replace_users_then_fail,
        )

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self.assertEqual(sentinel.read_bytes(), b"external")
        self.assertFalse((detached_users / str(self.alice.user_id) / str(record_uuid)).exists())
        detached_existing = (
            detached_users / str(self.alice.user_id) / existing_id / existing_xlsx.name
        )
        self.assertEqual(detached_existing.read_bytes(), b"xlsx-history")
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reimbursements").fetchone()[0], 1)

    def test_cleanup_does_not_follow_replaced_owner_to_different_directory(self):
        record_uuid = UUID("22222222-2222-4222-8222-222222222222")
        owner_dir = self.data_dir / "users" / str(self.alice.user_id)
        detached_owner = self.root / "detached-owner"
        replacement_owner = self.root / "replacement-owner"
        replacement_record = replacement_owner / str(record_uuid)
        replacement_record.mkdir(parents=True)
        sentinel = replacement_record / "DO-NOT-DELETE"
        sentinel.write_bytes(b"external")

        def replace_owner_then_fail(_source, _destination, **_kwargs):
            owner_dir.rename(detached_owner)
            replacement_owner.rename(owner_dir)
            raise OSError("forced move failure")

        service = self._service_with(
            uuid_factory=lambda: record_uuid,
            move_directory=replace_owner_then_fail,
        )

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        moved_sentinel = owner_dir / str(record_uuid) / sentinel.name
        self.assertEqual(moved_sentinel.read_bytes(), b"external")
        self.assertFalse((detached_owner / str(record_uuid)).exists())
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM reimbursements").fetchone()[0], 0)

    def test_cleanup_keeps_replacement_swapped_after_root_is_opened(self):
        record_uuid = UUID("22222222-2222-4222-8222-222222222223")
        tmp_root = self.data_dir / "tmp"
        replacement = self.root / "unowned-replacement"
        replacement.mkdir()
        sentinel_name = "DO-NOT-DELETE"
        (replacement / sentinel_name).write_bytes(b"external")

        def swap_cleanup(name, *, dir_fd, root_fd=None):
            os.rename(
                name,
                "detached-request",
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            replacement.rename(tmp_root / name)
            if root_fd is None:
                shutil.rmtree(name, dir_fd=dir_fd)

        service = self._service_with(
            uuid_factory=lambda: record_uuid,
            pdf_exporter=mock.Mock(side_effect=RuntimeError("forced failure")),
            remove_tree=swap_cleanup,
        )

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        sentinels = list(tmp_root.rglob(sentinel_name))
        self.assertEqual(len(sentinels), 1)
        self.assertEqual(sentinels[0].read_bytes(), b"external")

    def test_cleanup_does_not_unlink_leaf_replaced_after_identity_check(self):
        record_uuid = UUID("22222222-2222-4222-8222-222222222225")
        work_dir = self.data_dir / "tmp" / str(record_uuid)
        replacement = self.root / "unowned-replacement.xlsx"
        replacement.write_bytes(b"external")
        replacement_metadata = replacement.stat()
        armed = False
        real_stat = os.stat

        def arm_cleanup(_name, **_kwargs):
            nonlocal armed
            armed = True

        def swap_after_stat(path, *args, **kwargs):
            nonlocal armed
            metadata = real_stat(path, *args, **kwargs)
            if armed and path == "相同显示名.xlsx":
                dir_fd = kwargs["dir_fd"]
                os.rename(
                    path,
                    "detached-leaf.xlsx",
                    src_dir_fd=dir_fd,
                    dst_dir_fd=dir_fd,
                )
                replacement.rename(work_dir / path)
                armed = False
            return metadata

        service = self._service_with(
            uuid_factory=lambda: record_uuid,
            pdf_exporter=mock.Mock(side_effect=RuntimeError("forced failure")),
            remove_tree=arm_cleanup,
        )

        with mock.patch("reimbursements.os.stat", side_effect=swap_after_stat):
            with self.assertRaises(ReimbursementGenerationError):
                service.generate(self.alice, valid_payload(), [])

        identities = [candidate.lstat() for candidate in self.data_dir.rglob("*")]
        self.assertTrue(
            replacement.exists()
            or any(os.path.samestat(replacement_metadata, item) for item in identities)
        )

    def test_cleanup_does_not_rmdir_root_replaced_after_identity_check(self):
        record_uuid = UUID("22222222-2222-4222-8222-222222222226")
        work_dir = self.data_dir / "tmp" / str(record_uuid)
        replacement = self.root / "unowned-empty-directory"
        replacement.mkdir()
        replacement_metadata = replacement.stat()
        armed = False
        real_stat = os.stat

        def arm_cleanup(_name, **_kwargs):
            nonlocal armed
            armed = True

        def swap_after_stat(path, *args, **kwargs):
            nonlocal armed
            metadata = real_stat(path, *args, **kwargs)
            if armed and path == str(record_uuid):
                work_dir.rename(self.root / "detached-request-after-stat")
                replacement.rename(work_dir)
                armed = False
            return metadata

        service = self._service_with(
            uuid_factory=lambda: record_uuid,
            pdf_exporter=mock.Mock(side_effect=RuntimeError("forced failure")),
            remove_tree=arm_cleanup,
        )

        with mock.patch("reimbursements.os.stat", side_effect=swap_after_stat):
            with self.assertRaises(ReimbursementGenerationError):
                service.generate(self.alice, valid_payload(), [])

        identities = [candidate.lstat() for candidate in self.data_dir.rglob("*")]
        self.assertTrue(
            replacement.exists()
            or any(os.path.samestat(replacement_metadata, item) for item in identities)
        )

    def test_partial_move_failure_removes_the_new_final_directory(self):
        def move_then_fail(source, destination, *, src_dir_fd, dst_dir_fd):
            os.rename(
                source.name,
                destination.name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            raise OSError("move result uncertain")

        service = self._service_with(move_directory=move_then_fail)
        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_move_hook_cannot_swap_validated_workbook_to_external_hardlink(self):
        external = self.root / "external-before-move.xlsx"
        external.write_bytes(b"external-target")

        def swap_then_move(source, destination, *, src_dir_fd, dst_dir_fd):
            workbook = next(Path(source).rglob("*.xlsx"))
            workbook.unlink()
            workbook.hardlink_to(external)
            os.rename(
                source.name,
                destination.name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        service = self._service_with(move_directory=swap_then_move)

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self.assertEqual(external.read_bytes(), b"external-target")
        self.assertEqual(external.stat().st_nlink, 1)
        self._assert_no_history_or_artifacts()

    def test_move_hook_cannot_swap_validated_pdf_to_external_hardlink(self):
        external = self.root / "external-before-move.pdf"
        external.write_bytes(b"external-target")

        def swap_then_move(source, destination, *, src_dir_fd, dst_dir_fd):
            pdf = next(Path(source).rglob("*.pdf"))
            pdf.unlink()
            pdf.hardlink_to(external)
            os.rename(
                source.name,
                destination.name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        service = self._service_with(move_directory=swap_then_move)

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self.assertEqual(external.read_bytes(), b"external-target")
        self.assertEqual(external.stat().st_nlink, 1)
        self._assert_no_history_or_artifacts()

    def test_database_commit_then_error_compensates_row_before_removing_files(self):
        database = self.database

        class CommitThenRaiseDatabase:
            path = database.path

            def connect(self):
                return database.connect()

            @contextmanager
            def transaction(self, immediate=False):
                with database.transaction(immediate=immediate) as connection:
                    yield connection
                raise sqlite3.OperationalError("commit result uncertain")

        service = ReimbursementService(
            CommitThenRaiseDatabase(),
            self.config,
            workbook_generator=self.generator,
            pdf_exporter=self.exporter,
        )

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_cleanup_failure_does_not_mask_primary_error_or_touch_caller_files(self):
        record_uuid = UUID("12345678-1234-4234-8234-123456789abc")
        screenshot = self.root / "caller.png"
        screenshot.write_bytes(b"caller")
        self.exporter.side_effect = RuntimeError("payload-name-token-private-path")

        def cleanup_fails(_name, **_kwargs):
            raise OSError("cleanup-private-path")

        service = self._service_with(
            uuid_factory=lambda: record_uuid,
            remove_tree=cleanup_fails,
        )
        with self.assertLogs("reimbursements", level="ERROR") as captured:
            with self.assertRaisesRegex(
                ReimbursementGenerationError, "^生成报销文件失败，请稍后重试$"
            ):
                service.generate(self.alice, valid_payload(), [screenshot])

        combined = "\n".join(captured.output)
        self.assertIn(str(record_uuid), combined)
        self.assertIn("RuntimeError", combined)
        self.assertIn("OSError", combined)
        for secret in (
            "payload-name-token-private-path",
            "cleanup-private-path",
            self.alice.real_name,
            str(screenshot),
            str(self.template),
            self.alice.csrf_token,
        ):
            self.assertNotIn(secret, combined)
        self.assertEqual(self.template.read_bytes(), b"template")
        self.assertEqual(screenshot.read_bytes(), b"caller")

    def test_existing_tmp_uuid_collision_is_not_deleted_or_overwritten(self):
        record_uuid = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        existing = self.data_dir.resolve() / "tmp" / str(record_uuid)
        existing.mkdir(parents=True)
        marker = existing / "other-request.txt"
        marker.write_bytes(b"keep")
        service = self._service_with(uuid_factory=lambda: record_uuid)

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self.assertEqual(marker.read_bytes(), b"keep")
        self.generator.assert_not_called()

    def test_existing_final_uuid_collision_is_not_deleted_or_overwritten(self):
        record_uuid = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        existing = (
            self.data_dir.resolve()
            / "users"
            / str(self.alice.user_id)
            / str(record_uuid)
        )
        existing.mkdir(parents=True)
        marker = existing / "other-record.txt"
        marker.write_bytes(b"keep")
        service = self._service_with(uuid_factory=lambda: record_uuid)

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self.assertEqual(marker.read_bytes(), b"keep")
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM reimbursements").fetchone()[0],
                0,
            )

    def test_list_active_and_trash_are_owner_scoped_and_stably_newest_first(self):
        alice_old = "10000000-0000-4000-8000-000000000001"
        alice_same_low = "10000000-0000-4000-8000-000000000002"
        alice_same_high = "10000000-0000-4000-8000-000000000003"
        alice_trash = "10000000-0000-4000-8000-000000000004"
        bob_new = "20000000-0000-4000-8000-000000000001"
        self._insert_record(
            self.alice.user_id, alice_old, created_at="2026-09-07T00:00:00+00:00"
        )
        self._insert_record(
            self.alice.user_id, alice_same_low, created_at="2026-09-08T00:00:00+00:00"
        )
        self._insert_record(
            self.alice.user_id, alice_same_high, created_at="2026-09-08T00:00:00+00:00"
        )
        self._insert_record(
            self.alice.user_id,
            alice_trash,
            created_at="2026-09-09T00:00:00+00:00",
            deleted_at="2026-09-09T01:00:00+00:00",
        )
        self._insert_record(
            self.bob.user_id, bob_new, created_at="2026-09-10T00:00:00+00:00"
        )

        active = self.service.list_active(self.alice.user_id)
        trash = self.service.list_trash(self.alice.user_id)

        self.assertTrue(all(isinstance(item, ReimbursementRecord) for item in active + trash))
        self.assertEqual(
            [item.id for item in active],
            [alice_same_high, alice_same_low, alice_old],
        )
        self.assertEqual([item.id for item in trash], [alice_trash])
        self.assertTrue(all(item.user_id == self.alice.user_id for item in active + trash))

    def test_owned_file_requires_matching_owner_kind_and_active_record(self):
        active_id = "30000000-0000-4000-8000-000000000001"
        deleted_id = "30000000-0000-4000-8000-000000000002"
        active_xlsx, active_pdf = self._insert_record(
            self.alice.user_id, active_id, created_at="2026-09-08T00:00:00+00:00"
        )
        deleted_xlsx, _ = self._insert_record(
            self.alice.user_id,
            deleted_id,
            created_at="2026-09-07T00:00:00+00:00",
            deleted_at="2026-09-08T00:00:00+00:00",
        )

        with self.service.owned_file(
            self.alice.user_id, active_id, "xlsx"
        ) as owned_xlsx:
            self.assertEqual(owned_xlsx.stream.read(), active_xlsx.read_bytes())
            self.assertEqual(owned_xlsx.display_name, active_xlsx.name)
        with self.service.owned_file(
            self.alice.user_id, active_id, "pdf"
        ) as owned_pdf:
            self.assertEqual(owned_pdf.stream.read(), active_pdf.read_bytes())
            self.assertEqual(owned_pdf.display_name, active_pdf.name)
        for user_id, record_id, kind in (
            (self.bob.user_id, active_id, "xlsx"),
            (self.alice.user_id, active_id, "zip"),
            (self.alice.user_id, deleted_id, "xlsx"),
        ):
            with self.subTest(user_id=user_id, record_id=record_id, kind=kind):
                with self.assertRaisesRegex(ReimbursementNotFound, "^报销记录不存在$"):
                    self.service.owned_file(user_id, record_id, kind)
        with self.service.owned_file(
            self.alice.user_id, deleted_id, "xlsx", include_deleted=True
        ) as deleted_file:
            self.assertEqual(deleted_file.stream.read(), deleted_xlsx.read_bytes())

    def test_owned_file_rejects_unsafe_missing_and_cross_record_paths(self):
        first_id = "40000000-0000-4000-8000-000000000001"
        second_id = "40000000-0000-4000-8000-000000000002"
        first_xlsx, _ = self._insert_record(
            self.alice.user_id, first_id, created_at="2026-09-08T00:00:00+00:00"
        )
        second_xlsx, _ = self._insert_record(
            self.alice.user_id, second_id, created_at="2026-09-07T00:00:00+00:00"
        )
        external = self.root / "external-owned-file.xlsx"
        external.write_bytes(b"external")
        symlink = first_xlsx.parent / "linked.xlsx"
        symlink.symlink_to(external)
        directory = first_xlsx.parent / "directory.xlsx"
        directory.mkdir()
        cases = (
            str(external),
            second_xlsx.relative_to(self.data_dir.resolve()).as_posix(),
            symlink.relative_to(self.data_dir.resolve()).as_posix(),
            directory.relative_to(self.data_dir.resolve()).as_posix(),
            f"users/{self.alice.user_id}/{first_id}/missing.xlsx",
            f"users/{self.alice.user_id}/{first_id}/inside/../history.xlsx",
        )

        for stored_path in cases:
            with self.subTest(stored_path=stored_path):
                with self.database.transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE reimbursements SET xlsx_path = ? WHERE id = ?",
                        (stored_path, first_id),
                    )
                with self.assertRaises(ReimbursementNotFound) as captured:
                    self.service.owned_file(self.alice.user_id, first_id, "xlsx")
                self.assertEqual(str(captured.exception), "报销记录不存在")

        self.assertEqual(external.read_bytes(), b"external")

    def test_owned_file_rejects_external_hardlink(self):
        record_id = "70000000-0000-4000-8000-000000000001"
        xlsx, _ = self._insert_record(
            self.alice.user_id, record_id, created_at="2026-09-08T00:00:00+00:00"
        )
        external = self.root / "external-owned-hardlink.xlsx"
        external.write_bytes(b"external")
        xlsx.unlink()
        xlsx.hardlink_to(external)

        with self.assertRaises(ReimbursementNotFound):
            self.service.owned_file(self.alice.user_id, record_id, "xlsx")

        self.assertEqual(external.read_bytes(), b"external")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is not supported")
    def test_owned_file_rejects_fifo_without_blocking(self):
        record_id = "70000000-0000-4000-8000-000000000004"
        xlsx, _ = self._insert_record(
            self.alice.user_id,
            record_id,
            created_at="2026-09-08T00:00:00+00:00",
        )
        xlsx.unlink()
        os.mkfifo(xlsx, 0o600)
        done = threading.Event()
        errors = []

        def read_owned_file():
            try:
                self.service.owned_file(self.alice.user_id, record_id, "xlsx")
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        reader = threading.Thread(target=read_owned_file, daemon=True)
        reader.start()
        writer_fd = None
        try:
            completed_without_writer = done.wait(0.2)
            if not completed_without_writer:
                writer_fd = os.open(xlsx, os.O_WRONLY | os.O_NONBLOCK)
            self.assertTrue(done.wait(1), "owned_file blocked while opening a FIFO")
        finally:
            if writer_fd is not None:
                os.close(writer_fd)
            reader.join(1)

        self.assertTrue(completed_without_writer)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReimbursementNotFound)

    def test_owned_file_rejects_unsafe_pdf_display_name_from_database(self):
        record_id = "70000000-0000-4000-8000-000000000003"
        _, pdf = self._insert_record(
            self.alice.user_id, record_id, created_at="2026-09-08T00:00:00+00:00"
        )
        unsafe_pdf = pdf.with_name("unsafe\r\nInjected.pdf")
        pdf.rename(unsafe_pdf)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reimbursements SET pdf_path = ? WHERE id = ?",
                (unsafe_pdf.relative_to(self.data_dir.resolve()).as_posix(), record_id),
            )

        with self.assertRaises(ReimbursementNotFound):
            with self.service.owned_file(self.alice.user_id, record_id, "pdf"):
                pass

    def test_owned_file_stream_remains_bound_after_path_replacement(self):
        record_id = "70000000-0000-4000-8000-000000000002"
        xlsx, _ = self._insert_record(
            self.alice.user_id, record_id, created_at="2026-09-08T00:00:00+00:00"
        )
        original = xlsx.read_bytes()
        saved = self.root / "saved-original.xlsx"

        owned = self.service.owned_file(self.alice.user_id, record_id, "xlsx")
        try:
            xlsx.replace(saved)
            xlsx.write_bytes(b"attacker-replacement")
            self.assertEqual(owned.stream.read(), original)
            self.assertEqual(owned.record.id, record_id)
            self.assertEqual(owned.kind, "xlsx")
            self.assertEqual(owned.display_name, "history.xlsx")
            self.assertEqual(owned.size, len(original))
        finally:
            owned.close()

        self.assertTrue(owned.stream.closed)

    def test_create_server_exposes_service_without_eager_soffice_discovery(self):
        config_path = self.root / "server-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "template_path": str(self.template),
                    "data_dir": str(self.root / "server-data"),
                    "templates_dir": str(self.root / "templates"),
                    "static_dir": str(self.root / "static"),
                    "host": "127.0.0.1",
                    "port": 0,
                    "soffice_path": "",
                }
            ),
            encoding="utf-8",
        )

        class FakeServer:
            def __init__(self, address, handler):
                self.server_address = address
                self.handler = handler

        with mock.patch.object(app, "BoundedThreadingHTTPServer", FakeServer), mock.patch(
            "reimbursements.find_soffice",
            side_effect=AssertionError("soffice discovery must stay lazy"),
        ):
            server = app.create_server(config_path)

        self.assertIsInstance(server.reimbursement_service, ReimbursementService)
        self.assertIs(
            server.application.reimbursement_service,
            server.reimbursement_service,
        )

    def test_nested_outputs_keep_their_relative_paths_after_directory_move(self):
        def nested_generator(_template, work_dir, _payload, _images):
            nested = Path(work_dir) / "nested"
            nested.mkdir()
            xlsx = nested / "nested.xlsx"
            xlsx.write_bytes(b"xlsx")
            return GenerationResult(xlsx, 0, 0)

        def nested_exporter(_xlsx, work_dir, _soffice):
            pdf = Path(work_dir) / "nested" / "nested.pdf"
            pdf.write_bytes(b"pdf")
            return pdf

        service = self._service_with(
            workbook_generator=nested_generator,
            pdf_exporter=nested_exporter,
        )

        record = service.generate(self.alice, valid_payload(), [])

        with service.owned_file(
            self.alice.user_id, record.id, "xlsx"
        ) as owned_xlsx:
            self.assertEqual(owned_xlsx.stream.read(), b"xlsx")
        with service.owned_file(
            self.alice.user_id, record.id, "pdf"
        ) as owned_pdf:
            self.assertEqual(owned_pdf.stream.read(), b"pdf")
        self.assertIn("/nested/nested.xlsx", record.xlsx_path)
        self.assertIn("/nested/nested.pdf", record.pdf_path)

    def test_owned_file_rejects_wrong_extension_for_requested_kind(self):
        record_id = "50000000-0000-4000-8000-000000000001"
        _xlsx, pdf = self._insert_record(
            self.alice.user_id, record_id, created_at="2026-09-08T00:00:00+00:00"
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE reimbursements SET xlsx_path = ? WHERE id = ?",
                (pdf.relative_to(self.data_dir.resolve()).as_posix(), record_id),
            )

        with self.assertRaises(ReimbursementNotFound):
            self.service.owned_file(self.alice.user_id, record_id, "xlsx")

    def test_history_reads_explicitly_close_database_connections(self):
        record_id = "60000000-0000-4000-8000-000000000001"
        self._insert_record(
            self.alice.user_id, record_id, created_at="2026-09-08T00:00:00+00:00"
        )
        database = self.database
        observed = []

        class TrackingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.closed = False
                observed.append(self)

            def execute(self, *args, **kwargs):
                return self.connection.execute(*args, **kwargs)

            def close(self):
                self.closed = True
                self.connection.close()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return self.connection.__exit__(exc_type, exc, traceback)

        class TrackingDatabase:
            path = database.path

            def connect(self):
                return TrackingConnection(database.connect())

            def transaction(self, immediate=False):
                return database.transaction(immediate=immediate)

        service = ReimbursementService(
            TrackingDatabase(),
            self.config,
            workbook_generator=self.generator,
            pdf_exporter=self.exporter,
        )

        service.list_active(self.alice.user_id)
        with service.owned_file(self.alice.user_id, record_id, "xlsx"):
            pass

        self.assertEqual(len(observed), 2)
        self.assertTrue(all(connection.closed for connection in observed))

    def test_work_directory_open_failure_rolls_back_created_uuid_directory(self):
        record_uuid = UUID("33333333-3333-4333-8333-333333333331")
        record_id = str(record_uuid)
        real_open = os.open
        failed = False

        def fail_once(path, *args, **kwargs):
            nonlocal failed
            if path == record_id and not failed:
                failed = True
                raise OSError("forced work directory open failure")
            return real_open(path, *args, **kwargs)

        service = self._service_with(uuid_factory=lambda: record_uuid)
        with mock.patch("reimbursements.os.open", side_effect=fail_once):
            with self.assertRaises(ReimbursementGenerationError):
                service.generate(self.alice, valid_payload(), [])

        self.assertTrue(failed)
        self.assertFalse((self.data_dir / "tmp" / record_id).exists())
        self._assert_no_history_or_artifacts()

    def test_work_directory_identity_failure_rolls_back_and_closes_fd(self):
        record_uuid = UUID("33333333-3333-4333-8333-333333333332")
        record_id = str(record_uuid)
        real_open = os.open
        real_fstat = os.fstat
        record_fds = []
        fstat_calls = {}
        failed = False

        def tracking_open(path, *args, **kwargs):
            descriptor = real_open(path, *args, **kwargs)
            if path == record_id:
                record_fds.append(descriptor)
            return descriptor

        def fail_second_fstat(descriptor):
            nonlocal failed
            if descriptor in record_fds:
                count = fstat_calls.get(descriptor, 0) + 1
                fstat_calls[descriptor] = count
                if descriptor == record_fds[0] and count == 2 and not failed:
                    failed = True
                    raise OSError("forced work directory identity failure")
            return real_fstat(descriptor)

        service = self._service_with(uuid_factory=lambda: record_uuid)
        with mock.patch(
            "reimbursements.os.open", side_effect=tracking_open
        ), mock.patch("reimbursements.os.fstat", side_effect=fail_second_fstat):
            with self.assertRaises(ReimbursementGenerationError):
                service.generate(self.alice, valid_payload(), [])

        self.assertTrue(failed)
        self.assertFalse((self.data_dir / "tmp" / record_id).exists())
        self.assertGreaterEqual(len(record_fds), 2)
        for descriptor in record_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self._assert_no_history_or_artifacts()

    def test_final_claim_open_failure_rolls_back_created_uuid_directory(self):
        record_uuid = UUID("33333333-3333-4333-8333-333333333333")
        record_id = str(record_uuid)
        real_open = os.open
        record_open_count = 0
        failed = False

        def fail_second_record_open(path, *args, **kwargs):
            nonlocal failed, record_open_count
            if path == record_id:
                record_open_count += 1
                if record_open_count == 2 and not failed:
                    failed = True
                    raise OSError("forced final claim open failure")
            return real_open(path, *args, **kwargs)

        service = self._service_with(uuid_factory=lambda: record_uuid)
        with mock.patch(
            "reimbursements.os.open", side_effect=fail_second_record_open
        ):
            with self.assertRaises(ReimbursementGenerationError):
                service.generate(self.alice, valid_payload(), [])

        final_dir = self.data_dir / "users" / str(self.alice.user_id) / record_id
        self.assertTrue(failed)
        self.assertFalse(final_dir.exists())
        self._assert_no_history_or_artifacts()

    def test_final_claim_identity_failure_rolls_back_and_closes_fd(self):
        record_uuid = UUID("33333333-3333-4333-8333-333333333334")
        record_id = str(record_uuid)
        real_open = os.open
        real_fstat = os.fstat
        record_fds = []
        fstat_calls = {}
        failed = False

        def tracking_open(path, *args, **kwargs):
            descriptor = real_open(path, *args, **kwargs)
            if path == record_id:
                record_fds.append(descriptor)
            return descriptor

        def fail_final_second_fstat(descriptor):
            nonlocal failed
            if descriptor in record_fds:
                count = fstat_calls.get(descriptor, 0) + 1
                fstat_calls[descriptor] = count
                if len(record_fds) == 2 and descriptor == record_fds[1] and count == 2:
                    failed = True
                    raise OSError("forced final claim identity failure")
            return real_fstat(descriptor)

        service = self._service_with(uuid_factory=lambda: record_uuid)
        with mock.patch(
            "reimbursements.os.open", side_effect=tracking_open
        ), mock.patch("reimbursements.os.fstat", side_effect=fail_final_second_fstat):
            with self.assertRaises(ReimbursementGenerationError):
                service.generate(self.alice, valid_payload(), [])

        final_dir = self.data_dir / "users" / str(self.alice.user_id) / record_id
        self.assertTrue(failed)
        self.assertFalse(final_dir.exists())
        self.assertGreaterEqual(len(record_fds), 4)
        for descriptor in record_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self._assert_no_history_or_artifacts()

    def test_shared_directory_open_failures_do_not_remove_existing_directories(self):
        tmp_dir = self.data_dir / "tmp"
        users_dir = self.data_dir / "users"
        owner_dir = users_dir / str(self.alice.user_id)
        owner_dir.mkdir(parents=True)
        tmp_dir.mkdir(exist_ok=True)
        sentinels = []
        for directory, label in (
            (tmp_dir, "tmp"),
            (users_dir, "users"),
            (owner_dir, "owner"),
        ):
            sentinel = directory / f"{label}-sentinel"
            sentinel.write_bytes(label.encode("ascii"))
            sentinels.append((sentinel, label.encode("ascii")))

        for index, target in enumerate(("tmp", "users", str(self.alice.user_id)), 1):
            record_uuid = UUID(f"33333333-3333-4333-8333-33333333334{index}")
            real_open = os.open
            failed = False

            def fail_shared_once(path, *args, **kwargs):
                nonlocal failed
                if path == target and not failed:
                    failed = True
                    raise OSError("forced shared directory open failure")
                return real_open(path, *args, **kwargs)

            service = self._service_with(uuid_factory=lambda: record_uuid)
            with self.subTest(target=target), mock.patch(
                "reimbursements.os.open", side_effect=fail_shared_once
            ):
                with self.assertRaises(ReimbursementGenerationError):
                    service.generate(self.alice, valid_payload(), [])
                self.assertTrue(failed)
                for sentinel, content in sentinels:
                    self.assertEqual(sentinel.read_bytes(), content)

        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM reimbursements").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(list(self.data_dir.rglob("*.xlsx")), [])
        self.assertEqual(list(self.data_dir.rglob("*.pdf")), [])

    def test_generation_closes_all_opened_file_descriptors_on_success_and_failure(self):
        real_open = os.open

        for exporter_fails in (False, True):
            opened = []

            def tracking_open(*args, **kwargs):
                descriptor = real_open(*args, **kwargs)
                opened.append(descriptor)
                return descriptor

            exporter = self.exporter
            if exporter_fails:
                exporter = mock.Mock(side_effect=RuntimeError("forced failure"))
            service = self._service_with(pdf_exporter=exporter)
            with self.subTest(exporter_fails=exporter_fails), mock.patch(
                "reimbursements.os.open", side_effect=tracking_open
            ):
                if exporter_fails:
                    with self.assertRaises(ReimbursementGenerationError):
                        service.generate(self.alice, valid_payload(), [])
                else:
                    service.generate(self.alice, valid_payload(), [])

            self.assertTrue(opened)
            for descriptor in opened:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_isolate_entry_closes_quarantine_fd_when_post_rename_stat_fails(self):
        parent = self.root / "cleanup-parent"
        parent.mkdir()
        owned = parent / "owned.xlsx"
        owned.write_bytes(b"owned")
        metadata = owned.stat()
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        real_create = ReimbursementService._create_quarantine_directory
        real_stat = os.stat
        opened = []

        def tracking_create(directory_fd):
            name, descriptor = real_create(directory_fd)
            opened.append(descriptor)
            return name, descriptor

        def fail_isolated_stat(path, *args, **kwargs):
            if path == "entry":
                raise OSError("forced isolated stat failure")
            return real_stat(path, *args, **kwargs)

        try:
            with mock.patch.object(
                ReimbursementService,
                "_create_quarantine_directory",
                side_effect=tracking_create,
            ), mock.patch("reimbursements.os.stat", side_effect=fail_isolated_stat):
                with self.assertRaises(OSError):
                    ReimbursementService._isolate_entry(
                        parent_fd,
                        owned.name,
                        metadata,
                    )
        finally:
            os.close(parent_fd)

        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        retained = list(parent.rglob("entry"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), b"owned")

    def test_create_quarantine_closes_fd_when_identity_fstat_fails(self):
        parent = self.root / "cleanup-parent"
        parent.mkdir()
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        real_open = os.open
        real_fstat = os.fstat
        opened = []
        fstat_calls = {}

        def tracking_open(path, *args, **kwargs):
            descriptor = real_open(path, *args, **kwargs)
            if isinstance(path, str) and path.startswith(".reimbursement-cleanup-"):
                opened.append(descriptor)
            return descriptor

        def fail_second_fstat(descriptor):
            count = fstat_calls.get(descriptor, 0) + 1
            fstat_calls[descriptor] = count
            if descriptor in opened and count == 2:
                raise OSError("forced quarantine identity failure")
            return real_fstat(descriptor)

        try:
            with mock.patch(
                "reimbursements.os.open", side_effect=tracking_open
            ), mock.patch("reimbursements.os.fstat", side_effect=fail_second_fstat):
                with self.assertRaises(OSError):
                    ReimbursementService._create_quarantine_directory(parent_fd)
        finally:
            os.close(parent_fd)

        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_uuid_factory_must_return_version_four_before_path_creation(self):
        invalid_values = (
            "../escape",
            UUID("11111111-1111-1111-8111-111111111111"),
        )
        for value in invalid_values:
            generator = mock.Mock(side_effect=self.generator.side_effect)
            service = self._service_with(
                workbook_generator=generator,
                uuid_factory=lambda value=value: value,
            )
            with self.subTest(value=value), self.assertRaises(
                ReimbursementGenerationError
            ):
                service.generate(self.alice, valid_payload(), [])
            generator.assert_not_called()
        self.assertFalse((self.data_dir / "escape").exists())

    def test_owned_file_rejects_symlinked_record_root(self):
        record_id = "70000000-0000-4000-8000-000000000001"
        xlsx, pdf = self._insert_record(
            self.alice.user_id, record_id, created_at="2026-09-08T00:00:00+00:00"
        )
        record_root = xlsx.parent
        xlsx.unlink()
        pdf.unlink()
        record_root.rmdir()
        external_root = self.root / "external-record"
        external_root.mkdir()
        (external_root / "history.xlsx").write_bytes(b"external")
        (external_root / "history.pdf").write_bytes(b"external")
        record_root.symlink_to(external_root, target_is_directory=True)

        with self.assertRaises(ReimbursementNotFound):
            self.service.owned_file(self.alice.user_id, record_id, "xlsx")

    def test_owned_file_hides_database_lookup_failures(self):
        database = self.database

        class FailingLookupDatabase:
            path = database.path

            def connect(self):
                raise sqlite3.OperationalError("private database path")

            def transaction(self, immediate=False):
                return database.transaction(immediate=immediate)

        service = ReimbursementService(
            FailingLookupDatabase(),
            self.config,
            workbook_generator=self.generator,
            pdf_exporter=self.exporter,
        )

        with self.assertRaisesRegex(ReimbursementNotFound, "^报销记录不存在$"):
            service.owned_file(self.alice.user_id, "record", "xlsx")

    def test_database_insert_failure_removes_final_directory_and_leaves_no_row(self):
        database = self.database

        class RejectFirstTransactionDatabase:
            path = database.path

            def __init__(self):
                self.calls = 0

            def connect(self):
                return database.connect()

            @contextmanager
            def transaction(self, immediate=False):
                self.calls += 1
                if self.calls == 1:
                    raise sqlite3.IntegrityError("insert rejected")
                with database.transaction(immediate=immediate) as connection:
                    yield connection

        service = ReimbursementService(
            RejectFirstTransactionDatabase(),
            self.config,
            workbook_generator=self.generator,
            pdf_exporter=self.exporter,
        )

        with self.assertRaises(ReimbursementGenerationError):
            service.generate(self.alice, valid_payload(), [])

        self._assert_no_history_or_artifacts()

    def test_uncertain_database_cleanup_retains_final_directory_conservatively(self):
        database = self.database
        record_uuid = UUID("88888888-8888-4888-8888-888888888888")

        class UnavailableAfterCommitDatabase:
            path = database.path

            def __init__(self):
                self.calls = 0

            def connect(self):
                raise sqlite3.OperationalError("database unavailable")

            @contextmanager
            def transaction(self, immediate=False):
                self.calls += 1
                if self.calls == 1:
                    with database.transaction(immediate=immediate) as connection:
                        yield connection
                    raise sqlite3.OperationalError("commit result unavailable")
                raise sqlite3.OperationalError("compensation unavailable")

        service = ReimbursementService(
            UnavailableAfterCommitDatabase(),
            self.config,
            workbook_generator=self.generator,
            pdf_exporter=self.exporter,
            uuid_factory=lambda: record_uuid,
        )

        with self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(ReimbursementGenerationError):
                service.generate(self.alice, valid_payload(), [])

        final_dir = (
            self.data_dir.resolve()
            / "users"
            / str(self.alice.user_id)
            / str(record_uuid)
        )
        self.assertEqual((final_dir / "相同显示名.xlsx").read_bytes(), b"xlsx")
        self.assertEqual((final_dir / "相同显示名.pdf").read_bytes(), b"pdf")
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reimbursements WHERE id = ?", (str(record_uuid),)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["user_id"], self.alice.user_id)

    def test_database_id_collision_never_deletes_preexisting_owner_record(self):
        record_uuid = UUID("99999999-9999-4999-8999-999999999999")
        prior_values = (
            str(record_uuid),
            self.alice.user_id,
            "2026-01-01",
            "preexisting.xlsx",
            f"users/{self.alice.user_id}/{record_uuid}/preexisting.xlsx",
            f"users/{self.alice.user_id}/{record_uuid}/preexisting.pdf",
            "2026-01-01T00:00:00+00:00",
            None,
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO reimbursements(
                    id, user_id, reimbursement_date, display_name,
                    xlsx_path, pdf_path, created_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                prior_values,
            )
        final_dir = (
            self.data_dir.resolve()
            / "users"
            / str(self.alice.user_id)
            / str(record_uuid)
        )
        self.assertFalse(final_dir.exists())
        service = self._service_with(uuid_factory=lambda: record_uuid)

        with self.assertLogs("reimbursements", level="ERROR"):
            with self.assertRaises(ReimbursementGenerationError):
                service.generate(self.alice, valid_payload(), [])

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reimbursements WHERE id = ?", (str(record_uuid),)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(tuple(row), prior_values)
        self.assertFalse(final_dir.exists())


if __name__ == "__main__":
    unittest.main()
