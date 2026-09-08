"""Atomic private reimbursement generation and owner-scoped history reads."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import BinaryIO, Callable
from uuid import UUID, uuid4

from config import AppConfig
from database import Database
from generator import generate_workbook
from office import export_pdf, find_soffice
from sessions import AuthenticatedUser
from validation import safe_output_stem, validate_image_filename, validate_payload


_LOGGER = logging.getLogger(__name__)
_PUBLIC_GENERATION_ERROR = "生成报销文件失败，请稍后重试"
_PUBLIC_STORAGE_ERROR = "报销记录暂时无法删除，请稍后重试"
_MAX_DISPLAY_NAME_UTF8_BYTES = 180
_TRASH_RETENTION = timedelta(days=30)
_CLEANUP_ENTRY_NAME = "entry"
_PURGE_QUARANTINE_PREFIX = ".reimbursement-purge-"
_PURGE_QUARANTINE_AUTH_PURPOSE = b"reimbursement-purge-quarantine:v2"
_PURGE_COMPLETION_STATE = "complete"
_PURGE_COMPLETION_AUTH_PURPOSE = b"reimbursement-purge-completion:v1"
_PURGE_SERIALIZER = threading.Lock()
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class ReimbursementGenerationError(RuntimeError):
    """A stable generation failure safe to expose outside the service."""


class ReimbursementNotFound(FileNotFoundError):
    """A history lookup failure that does not disclose storage details."""


class ReimbursementStorageError(RuntimeError):
    """A stable owner-visible failure while permanently deleting storage."""


class _InsertError(Exception):
    def __init__(self, may_have_committed: bool, exception_name: str):
        super().__init__()
        self.may_have_committed = may_have_committed
        self.exception_name = exception_name


@dataclass(frozen=True)
class ReimbursementRecord:
    id: str
    user_id: int
    reimbursement_date: str | None
    display_name: str
    xlsx_path: str
    pdf_path: str
    created_at: str
    deleted_at: str | None


@dataclass
class OwnedReimbursementFile:
    record: ReimbursementRecord
    kind: str
    display_name: str
    size: int
    stream: BinaryIO

    def close(self) -> None:
        self.stream.close()

    def __enter__(self) -> "OwnedReimbursementFile":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


class ReimbursementService:
    """Generate private artifacts before atomically recording their owner."""

    def __init__(
        self,
        database: Database,
        config: AppConfig,
        *,
        workbook_generator: Callable = generate_workbook,
        pdf_exporter: Callable = export_pdf,
        soffice_finder: Callable[[str], Path] = find_soffice,
        uuid_factory: Callable = uuid4,
        clock: Callable[[], datetime] | None = None,
        move_directory: Callable | None = None,
        remove_tree: Callable | None = None,
        app_secret: bytes | None = None,
    ):
        if app_secret is not None and (
            not isinstance(app_secret, bytes) or len(app_secret) != 32
        ):
            raise ValueError("quarantine secret must be 32 bytes")
        self._database = database
        self._config = config
        self._generator = workbook_generator
        self._pdf_exporter = pdf_exporter
        self._soffice_finder = soffice_finder
        self._uuid_factory = uuid_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._move_directory = move_directory or self._rename_directory
        self._remove_tree = remove_tree
        self._quarantine_auth_key = (
            app_secret if app_secret is not None else os.urandom(32)
        )
        self._generation_slots = threading.BoundedSemaphore(
            config.max_concurrent_generations
        )

    def generate(
        self,
        user: AuthenticatedUser,
        raw_payload: dict,
        screenshots: list[Path],
    ) -> ReimbursementRecord:
        record_id = "unassigned"
        work_dir: Path | None = None
        final_dir: Path | None = None
        open_fds: list[int] = []
        tmp_fd: int | None = None
        owner_fd: int | None = None
        work_identity: tuple[int, int] | None = None
        final_claim_identity: tuple[int, int] | None = None
        moved = False
        try:
            record_uuid = self._uuid_factory()
            if not isinstance(record_uuid, UUID) or record_uuid.version != 4:
                raise ValueError("uuid factory must return UUID version 4")
            record_id = str(record_uuid)
            self._require_generation_user(user)
            payload = validate_payload(
                raw_payload,
                traveler=user.real_name,
                department=user.department,
            )
            output_stem = safe_output_stem(payload["date"], payload["traveler"])
            self._validated_display_name(f"{output_stem}.xlsx")
            payload["output_stem"] = output_stem
            image_paths = self._validated_screenshots(screenshots)
            data_dir = Path(self._config.data_dir).resolve()
            data_fd = self._open_data_root(data_dir)
            open_fds.append(data_fd)
            tmp_fd = self._open_or_create_directory(data_fd, "tmp")
            open_fds.append(tmp_fd)
            work_fd, work_identity = self._create_owned_directory(
                tmp_fd,
                record_id,
                record_id,
            )
            open_fds.append(work_fd)
            work_dir = data_dir / "tmp" / record_id

            with self._generation_slots:
                generation_result = self._generator(
                    Path(self._config.template_path),
                    work_dir,
                    payload,
                    image_paths,
                )
                (
                    generated_xlsx_path,
                    xlsx_work_relative,
                    xlsx_fd,
                    generated_xlsx_metadata,
                ) = self._open_validated_output(
                    getattr(generation_result, "path", None),
                    work_dir,
                    work_fd,
                    ".xlsx",
                )
                open_fds.append(xlsx_fd)
                configured_soffice = str(self._config.soffice_path or "").strip()
                soffice_path = (
                    Path(configured_soffice)
                    if configured_soffice
                    else Path(self._soffice_finder(""))
                )
                pdf_result = self._pdf_exporter(
                    generated_xlsx_path, work_dir, soffice_path
                )
                self._verify_output_identity(
                    work_fd,
                    xlsx_work_relative,
                    generated_xlsx_metadata,
                    xlsx_fd,
                )
                (
                    pdf_path,
                    pdf_work_relative,
                    pdf_fd,
                    pdf_metadata,
                ) = self._open_validated_output(
                    pdf_result,
                    work_dir,
                    work_fd,
                    ".pdf",
                )
                open_fds.append(pdf_fd)
                if os.path.samestat(generated_xlsx_metadata, pdf_metadata):
                    raise ValueError("generated outputs must be different files")
                display_name = self._validated_display_name(
                    generated_xlsx_path.name
                )
                self._validated_artifact_name(pdf_path.name, ".pdf")

            users_fd = self._open_or_create_directory(data_fd, "users")
            open_fds.append(users_fd)
            owner_fd = self._open_or_create_directory(
                users_fd, str(user.user_id)
            )
            open_fds.append(owner_fd)
            owner_dir = data_dir / "users" / str(user.user_id)
            final_dir = owner_dir / record_id
            final_claim_fd, final_claim_identity = self._create_owned_directory(
                owner_fd,
                record_id,
                record_id,
            )
            open_fds.append(final_claim_fd)
            self._verify_output_identity(
                work_fd,
                xlsx_work_relative,
                generated_xlsx_metadata,
                xlsx_fd,
            )
            self._verify_output_identity(
                work_fd,
                pdf_work_relative,
                pdf_metadata,
                pdf_fd,
            )
            self._move_directory(
                work_dir,
                final_dir,
                src_dir_fd=tmp_fd,
                dst_dir_fd=owner_fd,
            )
            moved = True
            final_fd = self._open_directory(owner_fd, record_id)
            open_fds.append(final_fd)
            if self._directory_identity(os.fstat(final_fd)) != work_identity:
                raise ValueError("moved record directory identity changed")
            self._verify_output_identity(
                final_fd,
                xlsx_work_relative,
                generated_xlsx_metadata,
                xlsx_fd,
            )
            self._verify_output_identity(
                final_fd,
                pdf_work_relative,
                pdf_metadata,
                pdf_fd,
            )

            record_relative = Path("users") / str(user.user_id) / record_id
            xlsx_relative = (record_relative / xlsx_work_relative).as_posix()
            pdf_relative = (record_relative / pdf_work_relative).as_posix()
            created_at = self._utc_now().isoformat()
            record = ReimbursementRecord(
                id=record_id,
                user_id=user.user_id,
                reimbursement_date=payload["date"],
                display_name=display_name,
                xlsx_path=xlsx_relative,
                pdf_path=pdf_relative,
                created_at=created_at,
                deleted_at=None,
            )
            self._insert(record)
            return record
        except Exception as error:
            exception_name = (
                error.exception_name
                if isinstance(error, _InsertError)
                else type(error).__name__
            )
            _LOGGER.error(
                "reimbursement generation failed record_id=%s exception=%s",
                record_id,
                exception_name,
            )
            database_allows_file_cleanup = True
            if moved and isinstance(error, _InsertError) and error.may_have_committed:
                database_allows_file_cleanup = self._compensate_insert(record)
            if not moved and tmp_fd is not None and work_identity is not None:
                self._cleanup_at(tmp_fd, record_id, {work_identity}, record_id)
            if owner_fd is not None and final_claim_identity is not None:
                owned_final_identities = {final_claim_identity}
                if work_identity is not None:
                    owned_final_identities.add(work_identity)
                if not moved or database_allows_file_cleanup:
                    self._cleanup_at(
                        owner_fd,
                        record_id,
                        owned_final_identities,
                        record_id,
                    )
            raise ReimbursementGenerationError(_PUBLIC_GENERATION_ERROR) from None
        finally:
            for descriptor in reversed(open_fds):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def list_active(self, user_id: int) -> list[ReimbursementRecord]:
        return self._list(user_id, deleted=False)

    def list_trash(self, user_id: int) -> list[ReimbursementRecord]:
        return self._list(user_id, deleted=True)

    def trash(
        self,
        user_id: int,
        record_id: str,
        now: datetime | None = None,
    ) -> ReimbursementRecord:
        self._require_owner_id(user_id)
        self._require_record_id(record_id)
        deleted_at = self._utc_time(now).isoformat()
        with self._database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """UPDATE reimbursements SET deleted_at = ?
                WHERE id = ? AND user_id = ? AND deleted_at IS NULL""",
                (deleted_at, record_id, user_id),
            )
            if updated.rowcount != 1:
                raise ReimbursementNotFound("报销记录不存在")
            row = connection.execute(
                "SELECT * FROM reimbursements WHERE id = ? AND user_id = ?",
                (record_id, user_id),
            ).fetchone()
        assert row is not None
        return self._record_from_row(row)

    def restore(self, user_id: int, record_id: str) -> ReimbursementRecord:
        self._require_owner_id(user_id)
        self._require_record_id(record_id)
        with closing(self._database.connect()) as connection:
            row = connection.execute(
                """SELECT * FROM reimbursements
                WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL""",
                (record_id, user_id),
            ).fetchone()
        if row is None or row["purge_claim"] is not None:
            raise ReimbursementNotFound("报销记录不存在")
        record = self._record_from_row(row)
        with self.owned_file(
            user_id,
            record_id,
            "xlsx",
            include_deleted=True,
        ), self.owned_file(
            user_id,
            record_id,
            "pdf",
            include_deleted=True,
        ):
            with self._database.transaction(immediate=True) as connection:
                updated = connection.execute(
                    """UPDATE reimbursements SET deleted_at = NULL
                    WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL
                    AND purge_claim IS NULL""",
                    (record_id, user_id),
                )
                if updated.rowcount != 1:
                    raise ReimbursementNotFound("报销记录不存在")
                row = connection.execute(
                    "SELECT * FROM reimbursements WHERE id = ? AND user_id = ?",
                    (record_id, user_id),
                ).fetchone()
        assert row is not None
        return self._record_from_row(row)

    def purge_one(self, user_id: int, record_id: str) -> None:
        self._require_owner_id(user_id)
        self._require_record_id(record_id)
        try:
            purged = self._purge_one(user_id, record_id)
        except ReimbursementStorageError:
            raise
        except Exception:
            raise ReimbursementStorageError(_PUBLIC_STORAGE_ERROR) from None
        if not purged:
            raise ReimbursementNotFound("报销记录不存在")

    def _purge_one(
        self,
        user_id: int,
        record_id: str,
        *,
        deleted_at_or_before: str | None = None,
    ) -> bool:
        with _PURGE_SERIALIZER:
            return self._purge_one_serialized(
                user_id,
                record_id,
                deleted_at_or_before=deleted_at_or_before,
            )

    def _purge_one_serialized(
        self,
        user_id: int,
        record_id: str,
        *,
        deleted_at_or_before: str | None = None,
    ) -> bool:
        eligibility = "deleted_at IS NOT NULL"
        parameters: tuple[object, ...] = (record_id, user_id)
        if deleted_at_or_before is not None:
            eligibility += " AND deleted_at <= ?"
            parameters += (deleted_at_or_before,)
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT * FROM reimbursements
                WHERE id = ? AND user_id = ? AND """ + eligibility,
                parameters,
            ).fetchone()
            if row is None:
                return False
            record = self._record_from_row(row)
            purge_claim = row["purge_claim"]
            if purge_claim is None:
                purge_claim = secrets.token_hex(32)
                claimed = connection.execute(
                    """UPDATE reimbursements SET purge_claim = ?
                    WHERE id = ? AND user_id = ? AND """
                    + eligibility
                    + " AND purge_claim IS NULL",
                    (purge_claim,) + parameters,
                )
                if claimed.rowcount != 1:
                    raise ReimbursementStorageError(_PUBLIC_STORAGE_ERROR)
            elif not self._valid_purge_claim(purge_claim):
                raise ReimbursementStorageError(_PUBLIC_STORAGE_ERROR)

        quarantine_name = self._purge_record_files(record, purge_claim)
        if quarantine_name is None:
            raise ReimbursementStorageError(_PUBLIC_STORAGE_ERROR)

        with self._database.transaction(immediate=True) as connection:
            if not self._purge_completion_ready_for_delete(
                record,
                purge_claim,
                quarantine_name,
            ):
                raise ReimbursementStorageError(_PUBLIC_STORAGE_ERROR)
            deleted = connection.execute(
                """DELETE FROM reimbursements
                WHERE id = ? AND user_id = ? AND """
                + eligibility
                + " AND purge_claim = ?",
                parameters + (purge_claim,),
            )
            if deleted.rowcount != 1:
                raise ReimbursementStorageError(_PUBLIC_STORAGE_ERROR)
        self._remove_purge_completion_marker(record, purge_claim, quarantine_name)
        return True

    def purge_expired(
        self,
        now: datetime | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> int:
        cutoff = (self._utc_time(now) - _TRASH_RETENTION).isoformat()
        with closing(self._database.connect()) as connection:
            candidates = connection.execute(
                """SELECT id, user_id FROM reimbursements
                WHERE deleted_at IS NOT NULL AND deleted_at <= ?
                ORDER BY deleted_at, id""",
                (cutoff,),
            ).fetchall()
        purged = 0
        for candidate in candidates:
            if stop_event is not None and stop_event.is_set():
                break
            record_id = candidate["id"]
            try:
                deleted = self._purge_one(
                    candidate["user_id"],
                    record_id,
                    deleted_at_or_before=cutoff,
                )
            except Exception as error:
                _LOGGER.error(
                    "expired reimbursement purge failed record_id=%s exception=%s",
                    record_id,
                    type(error).__name__,
                )
                continue
            if deleted:
                purged += 1
        return purged

    def owned_file(
        self,
        user_id: int,
        record_id: str,
        kind: str,
        include_deleted: bool = False,
    ) -> OwnedReimbursementFile:
        open_fds: list[int] = []
        file_fd: int | None = None
        try:
            self._require_owner_id(user_id)
            if (
                kind not in {"xlsx", "pdf"}
                or not isinstance(record_id, str)
                or not record_id
                or record_id in {".", ".."}
                or "/" in record_id
                or "\\" in record_id
                or not isinstance(include_deleted, bool)
            ):
                raise ValueError("invalid history lookup")
            deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
            with closing(self._database.connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM reimbursements WHERE id = ? AND user_id = ?"
                    + deleted_clause,
                    (record_id, user_id),
                ).fetchone()
            if row is None:
                raise ValueError("history not found")
            record = self._record_from_row(row)

            stored_value = row[f"{kind}_path"]
            if not isinstance(stored_value, str) or not stored_value:
                raise ValueError("invalid stored path")
            stored_path = Path(stored_value)
            if (
                stored_path.is_absolute()
                or ".." in stored_path.parts
                or stored_path.suffix.lower() != f".{kind}"
            ):
                raise ValueError("invalid stored path")

            expected_prefix = ("users", str(user_id), record_id)
            if stored_path.parts[:3] != expected_prefix:
                raise ValueError("stored path is outside record directory")
            relative_parts = stored_path.parts[3:]
            if not relative_parts:
                raise ValueError("invalid stored path")
            self._validated_artifact_name(relative_parts[-1], f".{kind}")
            data_fd = os.open(Path(self._config.data_dir).resolve(), _DIRECTORY_OPEN_FLAGS)
            open_fds.append(data_fd)
            current_fd = data_fd
            for component in expected_prefix:
                current_fd = self._open_directory(current_fd, component)
                open_fds.append(current_fd)
            for component in relative_parts[:-1]:
                current_fd = self._open_directory(current_fd, component)
                open_fds.append(current_fd)
            file_fd = os.open(
                relative_parts[-1],
                _FILE_OPEN_FLAGS,
                dir_fd=current_fd,
            )
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("stored path must be a private regular file")
            stream = os.fdopen(file_fd, "rb")
            file_fd = None
            return OwnedReimbursementFile(
                record=record,
                kind=kind,
                display_name=relative_parts[-1],
                size=metadata.st_size,
                stream=stream,
            )
        except Exception:
            raise ReimbursementNotFound("报销记录不存在") from None
        finally:
            if file_fd is not None:
                try:
                    os.close(file_fd)
                except OSError:
                    pass
            for descriptor in reversed(open_fds):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _list(self, user_id: int, *, deleted: bool) -> list[ReimbursementRecord]:
        self._require_owner_id(user_id)
        deleted_clause = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
        with closing(self._database.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM reimbursements WHERE user_id = ? AND "
                + deleted_clause
                + " ORDER BY created_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    @staticmethod
    def _require_owner_id(user_id: object) -> None:
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("valid user_id required")

    @staticmethod
    def _require_record_id(record_id: object) -> str:
        if not isinstance(record_id, str):
            raise ReimbursementNotFound("报销记录不存在")
        try:
            parsed = UUID(record_id)
        except (ValueError, AttributeError):
            raise ReimbursementNotFound("报销记录不存在") from None
        if parsed.version != 4 or str(parsed) != record_id:
            raise ReimbursementNotFound("报销记录不存在")
        return record_id

    @staticmethod
    def _valid_purge_claim(purge_claim: object) -> bool:
        return (
            isinstance(purge_claim, str)
            and len(purge_claim) == 64
            and all(character in "0123456789abcdef" for character in purge_claim)
        )

    def _purge_record_files(
        self,
        record: ReimbursementRecord,
        purge_claim: str,
    ) -> str | None:
        descriptors: list[int] = []
        try:
            if not self._valid_purge_claim(purge_claim):
                raise ValueError("invalid reimbursement purge claim")
            expected_prefix = ("users", str(record.user_id), record.id)
            for kind, stored_value in (
                ("xlsx", record.xlsx_path),
                ("pdf", record.pdf_path),
            ):
                if not isinstance(stored_value, str) or not stored_value:
                    raise ValueError("invalid stored path")
                stored_path = Path(stored_value)
                if (
                    stored_path.is_absolute()
                    or ".." in stored_path.parts
                    or stored_path.parts[:3] != expected_prefix
                    or len(stored_path.parts) < 4
                ):
                    raise ValueError("stored path is outside record directory")
                self._validated_artifact_name(stored_path.parts[-1], f".{kind}")

            data_fd = os.open(Path(self._config.data_dir).resolve(), _DIRECTORY_OPEN_FLAGS)
            descriptors.append(data_fd)
            users_fd = self._open_directory(data_fd, "users")
            descriptors.append(users_fd)
            owner_fd = self._open_directory(users_fd, str(record.user_id))
            descriptors.append(owner_fd)
            purge_markers = [
                (name, marker)
                for name in os.listdir(owner_fd)
                if (
                    marker := self._purge_marker_identity(
                        name,
                        record.id,
                        purge_claim,
                    )
                ) is not None
            ]
            if len(purge_markers) > 1:
                raise ValueError("ambiguous reimbursement purge quarantine")
            if purge_markers:
                marker_name, marker = purge_markers[0]
                state, identity, marker_identity = marker
                if state == _PURGE_COMPLETION_STATE:
                    assert marker_identity is not None
                    completed = self._verified_purge_completion_at(
                        owner_fd,
                        marker_name,
                        identity,
                        marker_identity,
                        record.id,
                    )
                    return marker_name if completed else None
                return self._resume_purge_at(
                    owner_fd,
                    marker_name,
                    identity,
                    record.id,
                    purge_claim,
                )
            record_fd = self._open_directory(owner_fd, record.id)
            descriptors.append(record_fd)
            identity = self._directory_identity(os.fstat(record_fd))
            quarantine_name = self._purge_quarantine_name(
                record.id,
                purge_claim,
                identity,
            )
            quarantine_fd = self._create_named_quarantine_directory(
                owner_fd,
                quarantine_name,
            )
            return self._resume_purge_at(
                owner_fd,
                quarantine_name,
                identity,
                record.id,
                purge_claim,
                quarantine_fd=quarantine_fd,
            )
        except Exception as error:
            _LOGGER.error(
                "reimbursement purge filesystem check failed record_id=%s exception=%s",
                record.id,
                type(error).__name__,
            )
            return None
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _purge_completion_ready_for_delete(
        self,
        record: ReimbursementRecord,
        purge_claim: str,
        completion_name: str,
    ) -> bool:
        descriptors: list[int] = []
        completion_fd: int | None = None
        try:
            completion = self._purge_completion_identity(
                completion_name,
                record.id,
                purge_claim,
            )
            if completion is None:
                return False
            _record_identity, expected_marker_identity = completion
            data_fd = os.open(
                Path(self._config.data_dir).resolve(),
                _DIRECTORY_OPEN_FLAGS,
            )
            descriptors.append(data_fd)
            users_fd = self._open_directory(data_fd, "users")
            descriptors.append(users_fd)
            owner_fd = self._open_directory(users_fd, str(record.user_id))
            descriptors.append(owner_fd)
            completion_metadata = os.stat(
                completion_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
            completion_fd = self._open_directory(owner_fd, completion_name)
            opened_metadata = os.fstat(completion_fd)
            if (
                not os.path.samestat(completion_metadata, opened_metadata)
                or self._directory_identity(opened_metadata)
                != expected_marker_identity
            ):
                return False
            with os.scandir(completion_fd) as entries:
                if next(entries, None) is not None:
                    return False
            try:
                os.stat(
                    record.id,
                    dir_fd=owner_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                return False
            current_completion = os.stat(
                completion_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
            return (
                os.path.samestat(opened_metadata, current_completion)
                and self._directory_identity(current_completion)
                == expected_marker_identity
            )
        except Exception as check_error:
            _LOGGER.error(
                "reimbursement purge final filesystem check failed "
                "record_id=%s exception=%s",
                record.id,
                type(check_error).__name__,
            )
            return False
        finally:
            if completion_fd is not None:
                try:
                    os.close(completion_fd)
                except OSError:
                    pass
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _remove_purge_completion_marker(
        self,
        record: ReimbursementRecord,
        purge_claim: str,
        quarantine_name: str,
    ) -> None:
        descriptors: list[int] = []
        quarantine_fd: int | None = None
        try:
            completion = self._purge_completion_identity(
                quarantine_name,
                record.id,
                purge_claim,
            )
            if completion is None:
                return
            _record_identity, expected_marker_identity = completion
            data_fd = os.open(
                Path(self._config.data_dir).resolve(),
                _DIRECTORY_OPEN_FLAGS,
            )
            descriptors.append(data_fd)
            users_fd = self._open_directory(data_fd, "users")
            descriptors.append(users_fd)
            owner_fd = self._open_directory(users_fd, str(record.user_id))
            descriptors.append(owner_fd)
            quarantine_metadata = os.stat(
                quarantine_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
            quarantine_fd = self._open_directory(owner_fd, quarantine_name)
            opened_metadata = os.fstat(quarantine_fd)
            if (
                not os.path.samestat(
                    quarantine_metadata,
                    opened_metadata,
                )
                or self._directory_identity(opened_metadata)
                != expected_marker_identity
                or os.listdir(quarantine_fd)
            ):
                return
            self._complete_empty_purge_at(
                owner_fd,
                quarantine_name,
                quarantine_metadata,
                quarantine_fd,
            )
        except Exception as cleanup_error:
            _LOGGER.error(
                "reimbursement purge marker cleanup failed record_id=%s exception=%s",
                record.id,
                type(cleanup_error).__name__,
            )
        finally:
            if quarantine_fd is not None:
                descriptor = quarantine_fd
                quarantine_fd = None
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _purge_quarantine_name(
        self,
        record_id: str,
        purge_claim: str,
        identity: tuple[int, int],
    ) -> str:
        if not self._valid_purge_claim(purge_claim):
            raise ValueError("invalid reimbursement purge claim")
        device, inode = identity
        device_hex = f"{device:x}"
        inode_hex = f"{inode:x}"
        mac = self._purge_quarantine_mac(
            record_id,
            purge_claim,
            device_hex,
            inode_hex,
        )
        return (
            f"{_PURGE_QUARANTINE_PREFIX}{record_id}-{purge_claim}-"
            f"{device_hex}-{inode_hex}-{mac}"
        )

    def _purge_quarantine_identity(
        self,
        name: str,
        record_id: str,
        purge_claim: str,
    ) -> tuple[int, int] | None:
        if not self._valid_purge_claim(purge_claim):
            return None
        prefix = f"{_PURGE_QUARANTINE_PREFIX}{record_id}-"
        if not isinstance(name, str) or not name.startswith(prefix):
            return None
        components = name[len(prefix):].split("-")
        if len(components) != 4 or components[0] != purge_claim:
            return None
        values = []
        for component in components[1:3]:
            if (
                not component
                or any(character not in "0123456789abcdef" for character in component)
            ):
                return None
            value = int(component, 16)
            if f"{value:x}" != component:
                return None
            values.append(value)
        supplied_mac = components[3]
        if (
            len(supplied_mac) != hashlib.sha256().digest_size * 2
            or any(
                character not in "0123456789abcdef" for character in supplied_mac
            )
        ):
            return None
        expected_mac = self._purge_quarantine_mac(
            record_id,
            purge_claim,
            components[1],
            components[2],
        )
        if not hmac.compare_digest(supplied_mac, expected_mac):
            return None
        return values[0], values[1]

    def _purge_marker_identity(
        self,
        name: str,
        record_id: str,
        purge_claim: str,
    ) -> tuple[str, tuple[int, int], tuple[int, int] | None] | None:
        pending_identity = self._purge_quarantine_identity(
            name,
            record_id,
            purge_claim,
        )
        if pending_identity is not None:
            return "pending", pending_identity, None
        completion = self._purge_completion_identity(
            name,
            record_id,
            purge_claim,
        )
        if completion is None:
            return None
        record_identity, marker_identity = completion
        return _PURGE_COMPLETION_STATE, record_identity, marker_identity

    def _purge_completion_name(
        self,
        record_id: str,
        purge_claim: str,
        record_identity: tuple[int, int],
        marker_identity: tuple[int, int],
    ) -> str:
        if not self._valid_purge_claim(purge_claim):
            raise ValueError("invalid reimbursement purge claim")
        record_device, record_inode = record_identity
        marker_device, marker_inode = marker_identity
        components = tuple(
            f"{value:x}"
            for value in (
                record_device,
                record_inode,
                marker_device,
                marker_inode,
            )
        )
        mac = self._purge_completion_mac(
            record_id,
            purge_claim,
            *components,
        )
        return (
            f"{_PURGE_QUARANTINE_PREFIX}{record_id}-"
            f"{_PURGE_COMPLETION_STATE}-{purge_claim}-"
            f"{'-'.join(components)}-{mac}"
        )

    def _purge_completion_identity(
        self,
        name: str,
        record_id: str,
        purge_claim: str,
    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
        if not self._valid_purge_claim(purge_claim):
            return None
        prefix = f"{_PURGE_QUARANTINE_PREFIX}{record_id}-"
        if not isinstance(name, str) or not name.startswith(prefix):
            return None
        components = name[len(prefix):].split("-")
        if (
            len(components) != 7
            or components[0] != _PURGE_COMPLETION_STATE
            or components[1] != purge_claim
        ):
            return None
        values = []
        for component in components[2:6]:
            if (
                not component
                or any(character not in "0123456789abcdef" for character in component)
            ):
                return None
            value = int(component, 16)
            if f"{value:x}" != component:
                return None
            values.append(value)
        supplied_mac = components[6]
        if (
            len(supplied_mac) != hashlib.sha256().digest_size * 2
            or any(
                character not in "0123456789abcdef" for character in supplied_mac
            )
        ):
            return None
        expected_mac = self._purge_completion_mac(
            record_id,
            purge_claim,
            *components[2:6],
        )
        if not hmac.compare_digest(supplied_mac, expected_mac):
            return None
        return (values[0], values[1]), (values[2], values[3])

    def _purge_completion_mac(
        self,
        record_id: str,
        purge_claim: str,
        record_device_hex: str,
        record_inode_hex: str,
        marker_device_hex: str,
        marker_inode_hex: str,
    ) -> str:
        message = b"\0".join(
            (
                _PURGE_COMPLETION_AUTH_PURPOSE,
                _PURGE_COMPLETION_STATE.encode("ascii"),
                record_id.encode("ascii"),
                purge_claim.encode("ascii"),
                record_device_hex.encode("ascii"),
                record_inode_hex.encode("ascii"),
                marker_device_hex.encode("ascii"),
                marker_inode_hex.encode("ascii"),
            )
        )
        return hmac.new(
            self._quarantine_auth_key,
            message,
            hashlib.sha256,
        ).hexdigest()

    def _purge_quarantine_mac(
        self,
        record_id: str,
        purge_claim: str,
        device_hex: str,
        inode_hex: str,
    ) -> str:
        message = b"\0".join(
            (
                _PURGE_QUARANTINE_AUTH_PURPOSE,
                record_id.encode("ascii"),
                purge_claim.encode("ascii"),
                device_hex.encode("ascii"),
                inode_hex.encode("ascii"),
            )
        )
        return hmac.new(
            self._quarantine_auth_key,
            message,
            hashlib.sha256,
        ).hexdigest()

    def _transition_purge_completion_at(
        self,
        owner_fd: int,
        pending_name: str,
        expected_metadata: os.stat_result,
        pending_fd: int,
        record_id: str,
        purge_claim: str,
        record_identity: tuple[int, int],
        *,
        expected_entries: tuple[str, ...] = (),
    ) -> str | None:
        opened_metadata = os.fstat(pending_fd)
        if (
            not os.path.samestat(expected_metadata, opened_metadata)
            or tuple(os.listdir(pending_fd)) != expected_entries
        ):
            return None
        marker_identity = self._directory_identity(opened_metadata)
        completion_name = self._purge_completion_name(
            record_id,
            purge_claim,
            record_identity,
            marker_identity,
        )
        try:
            os.stat(
                completion_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            return None
        current_metadata = os.stat(
            pending_name,
            dir_fd=owner_fd,
            follow_symlinks=False,
        )
        if (
            not os.path.samestat(expected_metadata, current_metadata)
            or not os.path.samestat(current_metadata, opened_metadata)
        ):
            return None
        os.rename(
            pending_name,
            completion_name,
            src_dir_fd=owner_fd,
            dst_dir_fd=owner_fd,
        )
        completion_metadata = os.stat(
            completion_name,
            dir_fd=owner_fd,
            follow_symlinks=False,
        )
        if (
            not os.path.samestat(opened_metadata, completion_metadata)
            or self._directory_identity(completion_metadata) != marker_identity
        ):
            return None
        return completion_name

    def _verified_purge_completion_at(
        self,
        owner_fd: int,
        completion_name: str,
        expected_record_identity: tuple[int, int],
        expected_marker_identity: tuple[int, int],
        record_id: str,
    ) -> bool:
        completion_fd: int | None = None
        record_fd: int | None = None
        filesystem_complete = False
        try:
            completion_metadata = os.stat(
                completion_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
            completion_fd = self._open_directory(owner_fd, completion_name)
            opened_metadata = os.fstat(completion_fd)
            if (
                not os.path.samestat(completion_metadata, opened_metadata)
                or self._directory_identity(opened_metadata)
                != expected_marker_identity
            ):
                return False
            try:
                os.stat(
                    record_id,
                    dir_fd=owner_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                return False
            completion_entries = os.listdir(completion_fd)
            if not completion_entries:
                filesystem_complete = True
                try:
                    os.stat(
                        record_id,
                        dir_fd=owner_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return True
                return False
            if completion_entries != [_CLEANUP_ENTRY_NAME]:
                return False
            record_metadata = os.stat(
                _CLEANUP_ENTRY_NAME,
                dir_fd=completion_fd,
                follow_symlinks=False,
            )
            record_fd = self._open_directory(
                completion_fd,
                _CLEANUP_ENTRY_NAME,
            )
            opened_record_metadata = os.fstat(record_fd)
            if (
                not os.path.samestat(record_metadata, opened_record_metadata)
                or self._directory_identity(opened_record_metadata)
                != expected_record_identity
            ):
                return False
            if self._remove_tree is not None:
                self._remove_tree(
                    _CLEANUP_ENTRY_NAME,
                    dir_fd=completion_fd,
                    root_fd=record_fd,
                )
            self._remove_directory_contents(record_fd, retain_directories=True)
            final_record_metadata = os.stat(
                _CLEANUP_ENTRY_NAME,
                dir_fd=completion_fd,
                follow_symlinks=False,
            )
            if not os.path.samestat(
                opened_record_metadata,
                final_record_metadata,
            ):
                return False
            if not self._retire_data_free_directory_at(
                completion_fd,
                _CLEANUP_ENTRY_NAME,
                final_record_metadata,
                record_fd,
                owner_fd,
            ):
                return False
            filesystem_complete = True
            current_completion = os.stat(
                completion_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
            if (
                not os.path.samestat(opened_metadata, current_completion)
                or os.listdir(completion_fd)
            ):
                return False
            try:
                os.stat(
                    record_id,
                    dir_fd=owner_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return True
            return False
        except Exception as cleanup_error:
            _LOGGER.error(
                "reimbursement cleanup failed record_id=%s exception=%s",
                record_id,
                type(cleanup_error).__name__,
            )
            return False
        finally:
            if record_fd is not None:
                if filesystem_complete:
                    try:
                        os.close(record_fd)
                    except OSError:
                        pass
                else:
                    os.close(record_fd)
            if completion_fd is not None:
                if filesystem_complete:
                    try:
                        os.close(completion_fd)
                    except OSError:
                        pass
                else:
                    os.close(completion_fd)

    def _resume_purge_at(
        self,
        owner_fd: int,
        quarantine_name: str,
        expected_identity: tuple[int, int],
        record_id: str,
        purge_claim: str,
        *,
        quarantine_fd: int | None = None,
    ) -> str | None:
        record_fd: int | None = None
        filesystem_complete = False
        try:
            quarantine_metadata = os.stat(
                quarantine_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
            if quarantine_fd is None:
                quarantine_fd = self._open_directory(owner_fd, quarantine_name)
            if not os.path.samestat(quarantine_metadata, os.fstat(quarantine_fd)):
                return None
            quarantine_entries = os.listdir(quarantine_fd)
            if not quarantine_entries:
                try:
                    record_metadata = os.stat(
                        record_id,
                        dir_fd=owner_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return None
                record_fd = self._open_directory(owner_fd, record_id)
                opened_metadata = os.fstat(record_fd)
                if (
                    not os.path.samestat(record_metadata, opened_metadata)
                    or self._directory_identity(opened_metadata)
                    != expected_identity
                ):
                    return None
                os.rename(
                    record_id,
                    _CLEANUP_ENTRY_NAME,
                    src_dir_fd=owner_fd,
                    dst_dir_fd=quarantine_fd,
                )
                isolated_metadata = os.stat(
                    _CLEANUP_ENTRY_NAME,
                    dir_fd=quarantine_fd,
                    follow_symlinks=False,
                )
                if (
                    not os.path.samestat(record_metadata, isolated_metadata)
                    or not os.path.samestat(opened_metadata, isolated_metadata)
                ):
                    return None
            elif quarantine_entries == [_CLEANUP_ENTRY_NAME]:
                record_metadata = os.stat(
                    _CLEANUP_ENTRY_NAME,
                    dir_fd=quarantine_fd,
                    follow_symlinks=False,
                )
                record_fd = self._open_directory(
                    quarantine_fd,
                    _CLEANUP_ENTRY_NAME,
                )
                opened_metadata = os.fstat(record_fd)
                if (
                    not os.path.samestat(record_metadata, opened_metadata)
                    or self._directory_identity(opened_metadata)
                    != expected_identity
                ):
                    return None
            else:
                return None
            completion_name = self._transition_purge_completion_at(
                owner_fd,
                quarantine_name,
                quarantine_metadata,
                quarantine_fd,
                record_id,
                purge_claim,
                expected_identity,
                expected_entries=(_CLEANUP_ENTRY_NAME,),
            )
            if completion_name is None:
                return None
            if self._remove_tree is not None:
                self._remove_tree(
                    _CLEANUP_ENTRY_NAME,
                    dir_fd=quarantine_fd,
                    root_fd=record_fd,
                )
            self._remove_directory_contents(record_fd, retain_directories=True)
            final_metadata = os.stat(
                _CLEANUP_ENTRY_NAME,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
            if not os.path.samestat(opened_metadata, final_metadata):
                return None
            if not self._retire_data_free_directory_at(
                quarantine_fd,
                _CLEANUP_ENTRY_NAME,
                final_metadata,
                record_fd,
                owner_fd,
            ):
                return None
            filesystem_complete = True
            completion_metadata = os.stat(
                completion_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
            if (
                not os.path.samestat(quarantine_metadata, completion_metadata)
                or not os.path.samestat(completion_metadata, os.fstat(quarantine_fd))
                or os.listdir(quarantine_fd)
            ):
                return None
            try:
                os.stat(
                    record_id,
                    dir_fd=owner_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return completion_name
            return None
        except Exception as cleanup_error:
            _LOGGER.error(
                "reimbursement cleanup failed record_id=%s exception=%s",
                record_id,
                type(cleanup_error).__name__,
            )
            return None
        finally:
            if record_fd is not None:
                descriptor = record_fd
                record_fd = None
                if filesystem_complete:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                else:
                    os.close(descriptor)
            if quarantine_fd is not None:
                descriptor = quarantine_fd
                quarantine_fd = None
                if filesystem_complete:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                else:
                    os.close(descriptor)

    def _retire_data_free_directory_at(
        self,
        source_parent_fd: int,
        name: str,
        expected_metadata: os.stat_result,
        directory_fd: int,
        tombstone_parent_fd: int,
    ) -> bool:
        tombstone_fd: int | None = None
        retired = False
        try:
            if not self._directory_tree_is_data_free(directory_fd):
                return False
            _tombstone_name, tombstone_fd, matches = self._isolate_entry(
                source_parent_fd,
                name,
                expected_metadata,
                quarantine_parent_fd=tombstone_parent_fd,
            )
            if not matches:
                return False
            isolated_metadata = os.stat(
                _CLEANUP_ENTRY_NAME,
                dir_fd=tombstone_fd,
                follow_symlinks=False,
            )
            if (
                not os.path.samestat(expected_metadata, isolated_metadata)
                or not os.path.samestat(isolated_metadata, os.fstat(directory_fd))
                or not self._directory_tree_is_data_free(directory_fd)
            ):
                return False
            retired = True
            return True
        finally:
            if tombstone_fd is not None:
                if retired:
                    try:
                        os.close(tombstone_fd)
                    except OSError:
                        pass
                else:
                    os.close(tombstone_fd)

    def _complete_empty_purge_at(
        self,
        owner_fd: int,
        quarantine_name: str,
        expected_metadata: os.stat_result,
        quarantine_fd: int,
    ) -> bool:
        tombstone_name: str | None = None
        completion_fd: int | None = None
        authenticated_directory_isolated = False
        try:
            current_metadata = os.stat(
                quarantine_name,
                dir_fd=owner_fd,
                follow_symlinks=False,
            )
            if (
                not os.path.samestat(expected_metadata, current_metadata)
                or not os.path.samestat(current_metadata, os.fstat(quarantine_fd))
                or os.listdir(quarantine_fd)
            ):
                return False
            tombstone_name, completion_fd, matches = self._isolate_entry(
                owner_fd,
                quarantine_name,
                current_metadata,
            )
            if not matches:
                return False
            isolated_metadata = os.stat(
                _CLEANUP_ENTRY_NAME,
                dir_fd=completion_fd,
                follow_symlinks=False,
            )
            if (
                not os.path.samestat(current_metadata, isolated_metadata)
                or not os.path.samestat(isolated_metadata, os.fstat(quarantine_fd))
                or os.listdir(quarantine_fd)
            ):
                return False
            # Portable POSIX has no directory unlink-by-fd. Keep the empty,
            # private tombstone instead of reopening a replacement race.
            authenticated_directory_isolated = True
            return True
        finally:
            if completion_fd is not None:
                descriptor = completion_fd
                completion_fd = None
                if authenticated_directory_isolated:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                else:
                    os.close(descriptor)

    @staticmethod
    def _record_from_row(row) -> ReimbursementRecord:
        return ReimbursementRecord(
            id=row["id"],
            user_id=row["user_id"],
            reimbursement_date=row["reimbursement_date"],
            display_name=row["display_name"],
            xlsx_path=row["xlsx_path"],
            pdf_path=row["pdf_path"],
            created_at=row["created_at"],
            deleted_at=row["deleted_at"],
        )

    @staticmethod
    def _require_generation_user(user: object) -> None:
        if (
            not isinstance(user, AuthenticatedUser)
            or user.role != "user"
            or type(user.user_id) is not int
            or user.user_id <= 0
        ):
            raise PermissionError("ordinary authenticated user required")

    @staticmethod
    def _validated_screenshots(screenshots: object) -> list[Path]:
        if not isinstance(screenshots, (list, tuple)):
            raise ValueError("screenshots must be a sequence")
        result: list[Path] = []
        for value in screenshots:
            if not isinstance(value, (str, os.PathLike)):
                raise ValueError("screenshot path required")
            path = Path(value)
            validate_image_filename(path.name)
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("screenshot must be a regular file")
            result.append(path)
        return result

    @classmethod
    def _open_validated_output(
        cls,
        value: object,
        work_dir: Path,
        work_fd: int,
        extension: str,
    ) -> tuple[Path, Path, int, os.stat_result]:
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("generated output path required")
        path = Path(value)
        if ".." in path.parts or path.suffix.lower() != extension:
            raise ValueError("invalid generated output path")
        try:
            relative = path.relative_to(work_dir)
        except ValueError as error:
            raise ValueError("generated output is outside request directory") from error
        if not relative.parts:
            raise ValueError("generated output path required")
        opened_directories: list[int] = []
        current_fd = work_fd
        try:
            for component in relative.parts[:-1]:
                current_fd = cls._open_directory(current_fd, component)
                opened_directories.append(current_fd)
            descriptor = os.open(
                relative.parts[-1],
                _FILE_OPEN_FLAGS,
                dir_fd=current_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("generated output must be a private regular file")
                return path, relative, descriptor, metadata
            except Exception:
                os.close(descriptor)
                raise
        finally:
            for descriptor in reversed(opened_directories):
                os.close(descriptor)

    @classmethod
    def _verify_output_identity(
        cls,
        record_fd: int,
        relative: Path,
        expected_metadata: os.stat_result,
        bound_fd: int,
    ) -> None:
        bound_metadata = os.fstat(bound_fd)
        if (
            bound_metadata.st_nlink != 1
            or not os.path.samestat(expected_metadata, bound_metadata)
        ):
            raise ValueError("generated output identity changed")
        opened_directories: list[int] = []
        current_fd = record_fd
        candidate_fd: int | None = None
        try:
            for component in relative.parts[:-1]:
                current_fd = cls._open_directory(current_fd, component)
                opened_directories.append(current_fd)
            candidate_fd = os.open(
                relative.parts[-1],
                _FILE_OPEN_FLAGS,
                dir_fd=current_fd,
            )
            candidate_metadata = os.fstat(candidate_fd)
            if (
                not stat.S_ISREG(candidate_metadata.st_mode)
                or candidate_metadata.st_nlink != 1
                or not os.path.samestat(expected_metadata, candidate_metadata)
            ):
                raise ValueError("generated output identity changed")
        finally:
            if candidate_fd is not None:
                os.close(candidate_fd)
            for descriptor in reversed(opened_directories):
                os.close(descriptor)

    @staticmethod
    def _validated_display_name(value: object) -> str:
        """Return a metadata-safe XLSX name capped at 180 UTF-8 bytes."""
        return ReimbursementService._validated_artifact_name(value, ".xlsx")

    @staticmethod
    def _validated_artifact_name(value: object, extension: str) -> str:
        if (
            not isinstance(value, str)
            or value in {"", ".", ".."}
            or "/" in value
            or "\\" in value
            or Path(value).suffix.lower() != extension
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("invalid artifact name")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid artifact name") from None
        if len(encoded) > _MAX_DISPLAY_NAME_UTF8_BYTES:
            raise ValueError("invalid artifact name")
        return value

    @staticmethod
    def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _open_data_root(path: Path) -> int:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, _DIRECTORY_OPEN_FLAGS)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ValueError("data root must be a directory")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _open_directory(parent_fd: int, name: str) -> int:
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ValueError("storage component must be a directory")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @classmethod
    def _open_or_create_directory(
        cls,
        parent_fd: int,
        name: str,
        *,
        exist_ok: bool = True,
    ) -> int:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            if not exist_ok:
                raise
        return cls._open_directory(parent_fd, name)

    def _create_owned_directory(
        self,
        parent_fd: int,
        name: str,
        record_id: str,
    ) -> tuple[int, tuple[int, int]]:
        descriptor: int | None = None
        identity: tuple[int, int] | None = None
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            created_metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(created_metadata.st_mode):
                raise ValueError("created storage entry must be a directory")
            identity = self._directory_identity(created_metadata)
            descriptor = self._open_directory(parent_fd, name)
            opened_metadata = os.fstat(descriptor)
            if not os.path.samestat(created_metadata, opened_metadata):
                raise ValueError("created directory identity changed")
            return descriptor, identity
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            if identity is not None:
                self._cleanup_at(parent_fd, name, {identity}, record_id)
            raise

    @staticmethod
    def _rename_directory(
        source: Path,
        destination: Path,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        os.rename(
            source.name,
            destination.name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def _utc_now(self) -> datetime:
        return self._utc_time(None)

    def _utc_time(self, supplied: datetime | None) -> datetime:
        value = self._clock() if supplied is None else supplied
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    def _insert(self, record: ReimbursementRecord) -> None:
        statement_applied = False
        try:
            with self._database.transaction(immediate=True) as connection:
                connection.execute(
                    """INSERT INTO reimbursements(
                        id, user_id, reimbursement_date, display_name,
                        xlsx_path, pdf_path, created_at, deleted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._record_values(record),
                )
                statement_applied = True
        except Exception as error:
            raise _InsertError(statement_applied, type(error).__name__) from error

    def _compensate_insert(self, record: ReimbursementRecord) -> bool:
        identity_clause = (
            "id = ? AND user_id = ? AND reimbursement_date IS ? AND "
            "display_name = ? AND xlsx_path = ? AND pdf_path = ? AND "
            "created_at = ? AND deleted_at IS ?"
        )
        values = self._record_values(record)
        try:
            with self._database.transaction(immediate=True) as connection:
                connection.execute(
                    "DELETE FROM reimbursements WHERE " + identity_clause,
                    values,
                )
        except Exception as compensation_error:
            _LOGGER.error(
                "reimbursement database cleanup failed record_id=%s exception=%s",
                record.id,
                type(compensation_error).__name__,
            )
        try:
            connection = self._database.connect()
            try:
                row = connection.execute(
                    "SELECT 1 FROM reimbursements WHERE " + identity_clause,
                    values,
                ).fetchone()
            finally:
                connection.close()
        except Exception as check_error:
            _LOGGER.error(
                "reimbursement database check failed record_id=%s exception=%s",
                record.id,
                type(check_error).__name__,
            )
            return False
        return row is None

    @staticmethod
    def _record_values(record: ReimbursementRecord) -> tuple:
        return (
            record.id,
            record.user_id,
            record.reimbursement_date,
            record.display_name,
            record.xlsx_path,
            record.pdf_path,
            record.created_at,
            record.deleted_at,
        )

    def _cleanup_at(
        self,
        parent_fd: int,
        name: str,
        expected_identities: set[tuple[int, int]],
        record_id: str,
        *,
        quarantine_name: str | None = None,
        retain_quarantine: bool = False,
    ) -> bool:
        root_fd: int | None = None
        quarantine_fd: int | None = None
        filesystem_complete = False
        try:
            root_fd = self._open_directory(parent_fd, name)
            root_metadata = os.fstat(root_fd)
            if self._directory_identity(root_metadata) not in expected_identities:
                return False
            quarantine_name, quarantine_fd, matches = self._isolate_entry(
                parent_fd,
                name,
                root_metadata,
                quarantine_name=quarantine_name,
            )
            if not matches:
                return False
            if self._remove_tree is not None:
                self._remove_tree(
                    _CLEANUP_ENTRY_NAME,
                    dir_fd=quarantine_fd,
                    root_fd=root_fd,
                )
            self._remove_directory_contents(root_fd)
            os.rmdir(_CLEANUP_ENTRY_NAME, dir_fd=quarantine_fd)
            filesystem_complete = True
            if not retain_quarantine:
                descriptor = quarantine_fd
                quarantine_fd = None
                try:
                    os.close(descriptor)
                except OSError:
                    return True
                try:
                    os.rmdir(quarantine_name, dir_fd=parent_fd)
                except OSError:
                    pass
            return True
        except FileNotFoundError:
            return False
        except Exception as cleanup_error:
            _LOGGER.error(
                "reimbursement cleanup failed record_id=%s exception=%s",
                record_id,
                type(cleanup_error).__name__,
            )
            return False
        finally:
            if quarantine_fd is not None:
                descriptor = quarantine_fd
                quarantine_fd = None
                if filesystem_complete:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                else:
                    os.close(descriptor)
            if root_fd is not None:
                descriptor = root_fd
                root_fd = None
                if filesystem_complete:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                else:
                    os.close(descriptor)

    @classmethod
    def _remove_directory_contents(
        cls,
        directory_fd: int,
        *,
        retain_directories: bool = False,
    ) -> None:
        for name in os.listdir(directory_fd):
            child_fd: int | None = None
            quarantine_fd: int | None = None
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(metadata.st_mode):
                    quarantine_name, quarantine_fd, matches = cls._isolate_entry(
                        directory_fd,
                        name,
                        metadata,
                    )
                    if not matches:
                        continue
                    os.unlink(_CLEANUP_ENTRY_NAME, dir_fd=quarantine_fd)
                    os.close(quarantine_fd)
                    quarantine_fd = None
                    if not retain_directories:
                        os.rmdir(quarantine_name, dir_fd=directory_fd)
                    continue

                child_fd = cls._open_directory(directory_fd, name)
                child_metadata = os.fstat(child_fd)
                if not os.path.samestat(metadata, child_metadata):
                    continue
                if retain_directories:
                    cls._remove_directory_contents(
                        child_fd,
                        retain_directories=True,
                    )
                    continue
                quarantine_name, quarantine_fd, matches = cls._isolate_entry(
                    directory_fd,
                    name,
                    child_metadata,
                )
                if not matches:
                    continue
                cls._remove_directory_contents(child_fd)
                os.rmdir(_CLEANUP_ENTRY_NAME, dir_fd=quarantine_fd)
                os.close(quarantine_fd)
                quarantine_fd = None
                os.rmdir(quarantine_name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            finally:
                if quarantine_fd is not None:
                    os.close(quarantine_fd)
                if child_fd is not None:
                    os.close(child_fd)

    @classmethod
    def _directory_tree_is_data_free(cls, directory_fd: int) -> bool:
        for name in os.listdir(directory_fd):
            child_fd: int | None = None
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(metadata.st_mode):
                    return False
                child_fd = cls._open_directory(directory_fd, name)
                if not os.path.samestat(metadata, os.fstat(child_fd)):
                    return False
                if not cls._directory_tree_is_data_free(child_fd):
                    return False
            except FileNotFoundError:
                return False
            finally:
                if child_fd is not None:
                    os.close(child_fd)
        return True

    @classmethod
    def _isolate_entry(
        cls,
        parent_fd: int,
        name: str,
        expected_metadata: os.stat_result,
        *,
        quarantine_name: str | None = None,
        quarantine_parent_fd: int | None = None,
    ) -> tuple[str, int, bool]:
        destination_parent_fd = (
            parent_fd if quarantine_parent_fd is None else quarantine_parent_fd
        )
        if quarantine_name is None:
            quarantine_name, quarantine_fd = cls._create_quarantine_directory(
                destination_parent_fd
            )
        else:
            quarantine_fd = cls._create_named_quarantine_directory(
                destination_parent_fd,
                quarantine_name,
            )
        renamed = False
        try:
            os.rename(
                name,
                _CLEANUP_ENTRY_NAME,
                src_dir_fd=parent_fd,
                dst_dir_fd=quarantine_fd,
            )
            renamed = True
            isolated_metadata = os.stat(
                _CLEANUP_ENTRY_NAME,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
            return (
                quarantine_name,
                quarantine_fd,
                os.path.samestat(expected_metadata, isolated_metadata),
            )
        except Exception:
            os.close(quarantine_fd)
            if not renamed:
                try:
                    os.rmdir(quarantine_name, dir_fd=destination_parent_fd)
                except OSError:
                    pass
            raise

    @classmethod
    def _create_named_quarantine_directory(
        cls,
        parent_fd: int,
        name: str,
    ) -> int:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        descriptor: int | None = None
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            descriptor = cls._open_directory(parent_fd, name)
            if not os.path.samestat(metadata, os.fstat(descriptor)):
                raise ValueError("cleanup directory identity changed")
            return descriptor
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            raise

    @classmethod
    def _create_quarantine_directory(cls, parent_fd: int) -> tuple[str, int]:
        for _attempt in range(10):
            name = f".reimbursement-cleanup-{uuid4().hex}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            descriptor: int | None = None
            try:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                descriptor = cls._open_directory(parent_fd, name)
                if not os.path.samestat(metadata, os.fstat(descriptor)):
                    raise ValueError("cleanup directory identity changed")
                return name, descriptor
            except Exception:
                if descriptor is not None:
                    os.close(descriptor)
                raise
        raise FileExistsError("could not reserve cleanup directory")
