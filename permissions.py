"""Feature access derived solely from the current signed license state."""

from dataclasses import dataclass
from calendar import monthrange
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from license_service import LicenseState


FREE_INVOICE_LIMIT = 6
INVOICE_LIMIT_MESSAGE = "普通用户每张报销单最多可导入 6 张发票，升级 Pro 后可解除限制。"
HISTORY_LIMIT_MESSAGE = "普通用户仅可查看最近 6 个月的报销记录，升级 Pro 后可查看全部历史记录。"
_HISTORY_TIMEZONE = ZoneInfo("Asia/Shanghai")


class InvoiceLimitExceeded(ValueError):
    def __init__(self):
        super().__init__(INVOICE_LIMIT_MESSAGE)


@dataclass(frozen=True)
class FeaturePermissions:
    invoice_limit: int | None
    history_retention_months: int | None
    reimbursement_stats: bool
    travel_trend: bool


def permissions_for_license(state: LicenseState) -> FeaturePermissions:
    if state.tier == "pro":
        return FeaturePermissions(None, None, True, True)
    return FeaturePermissions(FREE_INVOICE_LIMIT, 6, False, False)


def history_cutoff(months: int | None, *, now: datetime | None = None) -> datetime | None:
    if months is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("timezone-aware time required")
    local = current.astimezone(_HISTORY_TIMEZONE)
    month_index = local.year * 12 + local.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(local.day, monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day).astimezone(timezone.utc)


def history_visible(created_at: str, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    try:
        created = datetime.fromisoformat(created_at)
        return created.tzinfo is not None and created.utcoffset() is not None and created >= cutoff
    except (TypeError, ValueError, OverflowError):
        return False
