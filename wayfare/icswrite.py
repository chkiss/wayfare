"""Rendering a record as iCalendar text, for the reviewer to read.

The point is not interoperability — events are written to Google directly.
It is that a person reviewing a held record needs to see *everything* the tool
decided, in one block they can read top to bottom and paste elsewhere: the
title, both times with their zones, the location, the description and the
reminders. A form with six fields shows what is editable; this shows what was
concluded.

Built from the same event bodies that are sent to the calendar, deliberately,
so it cannot drift from what was actually created.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .render import to_google_events

#: RFC 5545 says lines are folded at 75 octets, continued with one space.
FOLD_AT = 74


def _escape(value: str) -> str:
    """Escape a TEXT value. Order matters: backslash first."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Split one long content line the way RFC 5545 requires."""
    if len(line) <= FOLD_AT:
        return [line]
    parts = [line[:FOLD_AT]]
    rest = line[FOLD_AT:]
    while rest:
        parts.append(" " + rest[: FOLD_AT - 1])
        rest = rest[FOLD_AT - 1 :]
    return parts


def _stamp(value: str) -> str:
    """An ISO local datetime as iCalendar's own format."""
    try:
        return datetime.fromisoformat(value).strftime("%Y%m%dT%H%M%S")
    except ValueError:
        return value.replace("-", "").replace(":", "")


def _time_lines(name: str, payload: dict) -> list[str]:
    """DTSTART/DTEND, carrying the zone as a TZID parameter.

    A local time with its zone named, never converted to UTC — the same rule
    the rest of the tool follows, and the reason the hour on screen matches
    the hour on the ticket.
    """
    if "date" in payload:
        return [f"{name};VALUE=DATE:{payload['date'].replace('-', '')}"]

    when = _stamp(str(payload.get("dateTime", "")))
    zone = payload.get("timeZone")
    if zone:
        return [f"{name};TZID={zone}:{when}"]
    return [f"{name}:{when}"]


def event_to_ics_lines(body: dict, uid: str) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{uid}"]
    lines.extend(_time_lines("DTSTART", body.get("start", {})))
    lines.extend(_time_lines("DTEND", body.get("end", body.get("start", {}))))
    lines.append(f"SUMMARY:{_escape(body.get('summary', ''))}")

    if body.get("location"):
        lines.append(f"LOCATION:{_escape(body['location'])}")
    if body.get("description"):
        lines.append(f"DESCRIPTION:{_escape(body['description'])}")

    reminders = body.get("reminders") or {}
    for override in reminders.get("overrides", []):
        minutes = override.get("minutes")
        if isinstance(minutes, int):
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{_escape(body.get('summary', 'Reminder'))}",
                    f"TRIGGER:-PT{minutes}M",
                    "END:VALARM",
                ]
            )

    lines.append("END:VEVENT")
    return lines


def record_to_ics(record, conventions: dict[str, Any] | None = None, uid_seed: str = "") -> str:
    """One record as a complete, pasteable VCALENDAR.

    Returns an empty string for a record with no usable time, which is the same
    thing that stops it reaching a calendar.
    """
    bodies = to_google_events(record, conventions)
    if not bodies:
        return ""

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//wayfare//EN",
        "CALSCALE:GREGORIAN",
    ]
    for index, body in enumerate(bodies):
        seed = uid_seed or "record"
        lines.extend(event_to_ics_lines(body, f"{seed}-{index}@wayfare"))
    lines.append("END:VCALENDAR")

    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    return "\r\n".join(folded) + "\r\n"
