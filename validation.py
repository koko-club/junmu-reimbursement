"""Validation and normalization for reimbursement form submissions."""

from decimal import Decimal, InvalidOperation
from typing import Any, Union


class ValidationError(ValueError):
    pass


_DETAIL_NUMERIC = (
    "public_amount",
    "public_transport_amount",
    "amount",
    "mileage",
    "driving_mileage",
    "toll",
    "lodging",
)
_DETAIL_TEXT = ("date", "origin", "destination", "transport")


def _text(value: Any, field: str, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be text")
    result = value.strip()
    if result.startswith(("=", "+", "-", "@")):
        raise ValidationError(f"{field} must not begin with a formula character")
    if required and not result:
        raise ValidationError(f"{field} is required")
    return result


def _date(value: Any, field: str) -> str:
    if value in (None, ""):
        return ""
    return _text(value, field)


def _number(value: Any, field: str, integer: bool = False) -> Union[int, float]:
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{field} must be a number")
    try:
        number = Decimal(str(value).strip()) if isinstance(value, str) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{field} must be a number")
    if not number.is_finite() or number < 0:
        raise ValidationError(f"{field} must be non-negative")
    if integer and number != number.to_integral_value():
        raise ValidationError(f"{field} must be an integer")
    if integer:
        return int(number)
    as_int = int(number)
    return as_int if number == as_int else float(number)


def _detail(row: Any, index: int) -> dict:
    if not isinstance(row, dict):
        raise ValidationError(f"row {index} must be an object")
    if not any(value not in (None, "") for value in row.values()):
        return {}

    normalized = {}
    for field in _DETAIL_TEXT:
        if field in row and row[field] not in (None, ""):
            normalized[field] = _text(row[field], f"row {index} {field}")
    required = {field: normalized.get(field, "") for field in ("origin", "destination")}
    if not all(required.values()):
        raise ValidationError(f"row {index} requires origin and destination together")
    if "date" in normalized:
        normalized["date"] = _date(normalized["date"], f"row {index} date")
    for field in _DETAIL_NUMERIC:
        if field in row and row[field] not in (None, ""):
            normalized[field] = _number(row[field], f"row {index} {field}")
    if "receipts" in row and row["receipts"] not in (None, ""):
        normalized["receipts"] = _number(row["receipts"], f"row {index} receipts", integer=True)
    return normalized


def validate_payload(raw: dict) -> dict:
    """Return normalized payload with exactly 11 detail rows or raise ValidationError."""
    if not isinstance(raw, dict):
        raise ValidationError("payload must be an object")
    result = {
        "date": _date(raw.get("date"), "date"),
        "department": _text(raw.get("department"), "department", required=True),
        "traveler": _text(raw.get("traveler"), "traveler", required=True),
        "reason": _text(raw.get("reason"), "reason", required=True),
        "days": _number(raw.get("days"), "days"),
        "allowance": _number(raw.get("allowance"), "allowance"),
    }
    rows = raw.get("rows", [])
    if not isinstance(rows, list) or len(rows) > 11:
        raise ValidationError("rows must contain at most 11 detail rows")
    normalized_rows = [_detail(row, index + 1) for index, row in enumerate(rows)]
    normalized_rows.extend({} for _ in range(11 - len(normalized_rows)))
    result["rows"] = normalized_rows
    result["receipt_count"] = sum(row.get("receipts", 0) for row in normalized_rows)
    return result


def validate_image_filename(filename: str) -> str:
    """Return a supported image filename, or raise ValidationError."""
    if not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename:
        raise ValidationError("invalid image filename")
    if any(ord(char) < 32 or ord(char) == 127 for char in filename):
        raise ValidationError("invalid image filename")
    if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
        raise ValidationError("unsupported image extension")
    return filename


def safe_output_stem(date_text: str, traveler: str) -> str:
    """Return a filesystem-safe, meaningful reimbursement workbook stem."""
    def clean(value: Any, fallback: str) -> str:
        value = str(value) if value is not None else ""
        value = "".join(char for char in value if ord(char) >= 32 and ord(char) != 127)
        value = value.replace("/", "").replace("\\", "").strip()
        # Do not allow a sanitized name to begin with a traversal marker.
        value = value.lstrip(".")
        return value or fallback

    return f"{clean(date_text, '未知日期')}-{clean(traveler, '未命名')}-差旅报销单"
