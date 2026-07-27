from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Madrid"


def resolve_timezone_name(timezone_name: str | None) -> str:
    candidate = (timezone_name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return DEFAULT_TIMEZONE
    return candidate


def _coerce_datetime(value: datetime | date | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, datetime.min.time(), tzinfo=dt_timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value


def format_local_datetime(
    value: datetime | date | str | None,
    timezone_name: str | None = None,
    fmt: str = "%d/%m/%Y %H:%M",
    default: str = "Sin registro",
) -> str:
    dt_value = _coerce_datetime(value)
    if dt_value is None:
        return default
    tz_name = resolve_timezone_name(timezone_name)
    try:
        local_dt = dt_value.astimezone(ZoneInfo(tz_name))
    except ZoneInfoNotFoundError:
        local_dt = dt_value.astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    return local_dt.strftime(fmt)
