"""Converting ticket times into instants, carefully."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schema import LocalTime


def to_utc(when: LocalTime | None) -> datetime | None:
    """Interpret a local wall-clock time in its own zone and return the instant.

    Returns None when the zone is unknown — deliberately, because guessing a
    zone is exactly the error this whole module exists to prevent.
    """
    if when is None or not when.timezone:
        return None
    try:
        zone = ZoneInfo(when.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return when.local.replace(tzinfo=zone).astimezone(timezone.utc)


def elapsed_hours(start: LocalTime | None, end: LocalTime | None) -> float | None:
    """Real elapsed hours between two zoned local times."""
    a, b = to_utc(start), to_utc(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def shift_days(when: LocalTime, days: int) -> LocalTime:
    """A copy of a local time moved by whole days, zone preserved."""
    return LocalTime(local=when.local + timedelta(days=days), timezone=when.timezone)


def format_local(when: LocalTime | None) -> str:
    """Human-readable rendering used in issue messages and the review screen."""
    if when is None:
        return "?"
    stamp = when.local.strftime("%Y-%m-%d %H:%M")
    return f"{stamp} {when.timezone}" if when.timezone else f"{stamp} (zone unknown)"
