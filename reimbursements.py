"""Atomic private reimbursement generation and owner-scoped history reads."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
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
_MAX_DISPLAY_NAME_UTF8_BYTES = 180
_CLEANUP_ENTRY_NAME = "entry"
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
    ):
        self._database = database
        self._config = config
        self._generator = workbook_generator
        self._pdf_exporter = pdf_exporter
        self._soffice_finder = soffice_finder
        self._uuid_factory = uuid_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._move_directory = move_directory or self._rename_directory
        self._remove_tree = remove_tree
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
            work_fd = self._open_or_create_directory(
                tmp_fd, record_id, exist_ok=False
            )
            open_fds.append(work_fd)
            work_identity = self._directory_identity(os.fstat(work_fd))
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
            final_claim_fd = self._open_or_create_directory(
                owner_fd, record_id, exist_ok=False
            )
            open_fds.append(final_claim_fd)
            final_claim_identity = self._directory_identity(os.fstat(final_claim_fd))
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
        value = self._clock()
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
    ) -> None:
        root_fd: int | None = None
        quarantine_fd: int | None = None
        try:
            root_fd = self._open_directory(parent_fd, name)
            root_metadata = os.fstat(root_fd)
            if self._directory_identity(root_metadata) not in expected_identities:
                return
            if self._remove_tree is not None:
                self._remove_tree(name, dir_fd=parent_fd, root_fd=root_fd)
            quarantine_name, quarantine_fd, matches = self._isolate_entry(
                parent_fd,
                name,
                root_metadata,
            )
            if not matches:
                return
            self._remove_directory_contents(root_fd)
            os.rmdir(_CLEANUP_ENTRY_NAME, dir_fd=quarantine_fd)
            os.close(quarantine_fd)
            quarantine_fd = None
            os.rmdir(quarantine_name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except Exception as cleanup_error:
            _LOGGER.error(
                "reimbursement cleanup failed record_id=%s exception=%s",
                record_id,
                type(cleanup_error).__name__,
            )
        finally:
            if quarantine_fd is not None:
                os.close(quarantine_fd)
            if root_fd is not None:
                os.close(root_fd)

    @classmethod
    def _remove_directory_contents(cls, directory_fd: int) -> None:
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
                    os.rmdir(quarantine_name, dir_fd=directory_fd)
                    continue

                child_fd = cls._open_directory(directory_fd, name)
                child_metadata = os.fstat(child_fd)
                if not os.path.samestat(metadata, child_metadata):
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
    def _isolate_entry(
        cls,
        parent_fd: int,
        name: str,
        expected_metadata: os.stat_result,
    ) -> tuple[str, int, bool]:
        quarantine_name, quarantine_fd = cls._create_quarantine_directory(parent_fd)
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
                    os.rmdir(quarantine_name, dir_fd=parent_fd)
                except OSError:
                    pass
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
