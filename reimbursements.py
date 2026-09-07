"""Atomic private reimbursement generation and owner-scoped history reads."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import shutil
import stat
import threading
from typing import Callable
from uuid import UUID, uuid4

from config import AppConfig
from database import Database
from generator import generate_workbook
from office import export_pdf, find_soffice
from sessions import AuthenticatedUser
from validation import validate_image_filename, validate_payload


_LOGGER = logging.getLogger(__name__)
_PUBLIC_GENERATION_ERROR = "生成报销文件失败，请稍后重试"
_MAX_DISPLAY_NAME_UTF8_BYTES = 180


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
        move_directory: Callable[[Path, Path], None] = os.replace,
        remove_tree: Callable[[Path], None] | None = None,
    ):
        self._database = database
        self._config = config
        self._generator = workbook_generator
        self._pdf_exporter = pdf_exporter
        self._soffice_finder = soffice_finder
        self._uuid_factory = uuid_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._move_directory = move_directory
        self._remove_tree = remove_tree or shutil.rmtree
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
        work_created = False
        final_claimed = False
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
            image_paths = self._validated_screenshots(screenshots)
            data_dir = Path(self._config.data_dir).resolve()
            tmp_root = self._controlled_directory(data_dir / "tmp", data_dir)
            work_dir = tmp_root / record_id
            work_dir.mkdir(mode=0o700, exist_ok=False)
            work_created = True

            with self._generation_slots:
                generation_result = self._generator(
                    Path(self._config.template_path),
                    work_dir,
                    payload,
                    image_paths,
                )
                generated_xlsx_path = self._validated_output(
                    getattr(generation_result, "path", None), work_dir, ".xlsx"
                )
                generated_xlsx_metadata = generated_xlsx_path.lstat()
                configured_soffice = str(self._config.soffice_path or "").strip()
                soffice_path = (
                    Path(configured_soffice)
                    if configured_soffice
                    else Path(self._soffice_finder(""))
                )
                pdf_result = self._pdf_exporter(
                    generated_xlsx_path, work_dir, soffice_path
                )
                xlsx_path = self._validated_output(
                    generated_xlsx_path, work_dir, ".xlsx"
                )
                if not os.path.samestat(generated_xlsx_metadata, xlsx_path.lstat()):
                    raise ValueError("generated workbook was replaced")
                pdf_path = self._validated_output(pdf_result, work_dir, ".pdf")
                if xlsx_path.samefile(pdf_path):
                    raise ValueError("generated outputs must be different files")
                xlsx_work_relative = xlsx_path.relative_to(work_dir)
                pdf_work_relative = pdf_path.relative_to(work_dir)
                display_name = self._validated_display_name(xlsx_path.name)

            owner_root = self._controlled_directory(data_dir / "users", data_dir)
            owner_dir = self._controlled_directory(
                owner_root / str(user.user_id), owner_root
            )
            final_dir = owner_dir / record_id
            final_dir.mkdir(mode=0o700, exist_ok=False)
            final_claimed = True
            self._move_directory(work_dir, final_dir)
            moved = True

            xlsx_relative = (final_dir / xlsx_work_relative).relative_to(data_dir).as_posix()
            pdf_relative = (final_dir / pdf_work_relative).relative_to(data_dir).as_posix()
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
            if moved and final_dir is not None and database_allows_file_cleanup:
                self._cleanup(final_dir, record_id)
            elif not moved and work_created and work_dir is not None:
                self._cleanup(work_dir, record_id)
            if not moved and final_claimed and final_dir is not None:
                self._cleanup(final_dir, record_id)
            raise ReimbursementGenerationError(_PUBLIC_GENERATION_ERROR) from None

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
    ) -> Path:
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

            data_dir = Path(self._config.data_dir).resolve()
            record_root = data_dir / "users" / str(user_id) / record_id
            for directory in (
                data_dir / "users",
                data_dir / "users" / str(user_id),
                record_root,
            ):
                metadata = directory.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError("record directory is not controlled")
            candidate = data_dir / stored_path
            relative = candidate.relative_to(record_root)
            if not relative.parts:
                raise ValueError("invalid stored path")
            current = record_root
            for part in relative.parts:
                current = current / part
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("stored path must not use symlinks")
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("stored path must be a regular file")
            candidate.resolve().relative_to(record_root.resolve())
            return candidate
        except Exception:
            raise ReimbursementNotFound("报销记录不存在") from None

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

    @staticmethod
    def _validated_output(value: object, work_dir: Path, extension: str) -> Path:
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
        current = work_dir
        for part in relative.parts:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("generated output must not use symlinks")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("generated output must be a regular file")
        try:
            path.resolve().relative_to(work_dir.resolve())
        except ValueError as error:
            raise ValueError("generated output is outside request directory") from error
        return path

    @staticmethod
    def _validated_display_name(value: object) -> str:
        """Return a metadata-safe XLSX name capped at 180 UTF-8 bytes."""
        if (
            not isinstance(value, str)
            or value in {"", ".", ".."}
            or "/" in value
            or "\\" in value
            or Path(value).suffix.lower() != ".xlsx"
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("invalid display name")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid display name") from None
        if len(encoded) > _MAX_DISPLAY_NAME_UTF8_BYTES:
            raise ValueError("invalid display name")
        return value

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    @classmethod
    def _controlled_directory(cls, path: Path, parent: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("storage directory is not controlled")
        resolved = path.resolve()
        try:
            resolved.relative_to(parent.resolve())
        except ValueError as error:
            raise ValueError("storage directory is outside data directory") from error
        return path

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

    def _cleanup(self, path: Path, record_id: str) -> None:
        try:
            if path.is_symlink():
                path.unlink()
            elif self._path_exists(path):
                self._remove_tree(path)
        except Exception as cleanup_error:
            _LOGGER.error(
                "reimbursement cleanup failed record_id=%s exception=%s",
                record_id,
                type(cleanup_error).__name__,
            )
