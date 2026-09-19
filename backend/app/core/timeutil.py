"""Timezone helpers. Everything is stored in UTC and rendered in local time."""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    return datetime.now(UTC)


def get_zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def ensure_aware(value: datetime) -> datetime:
    """Treat naive datetimes as UTC (SQLite loses tzinfo on round-trip)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def to_local(value: datetime, tz_name: str | None) -> datetime:
    return ensure_aware(value).astimezone(get_zone(tz_name))


def local_date(value: datetime, tz_name: str | None) -> date:
    return to_local(value, tz_name).date()


def combine_local(day: date, at: time, tz_name: str | None) -> datetime:
    """Build an aware UTC datetime from a local calendar date and wall time."""
    zone = get_zone(tz_name)
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)


def seconds_between(start: datetime, end: datetime | None = None) -> int:
    end = end or utcnow()
    return max(0, int((ensure_aware(end) - ensure_aware(start)).total_seconds()))


def humanize_duration(seconds: int, short: bool = False) -> str:
    """600 -> '10m', 5400 -> '1h 30m', 45 -> '45s'."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m" if minutes or not short else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def format_hours(seconds: int) -> str:
    """Decimal hours for reports and CSV export: 27900 -> '7.75'."""
    return f"{max(0, seconds) / 3600:.2f}"


def week_bounds(day: date) -> tuple[date, date]:
    """Monday-to-Sunday week containing ``day``."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month - timedelta(days=1)


def daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
