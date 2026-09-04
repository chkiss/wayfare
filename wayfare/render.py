"""Turning records into calendar events.

Titles and descriptions are the part of this tool that is purely personal
taste, so every template lives in one place and can be overridden from a JSON
file without touching the code:

    WAYFARE_CONVENTIONS=/path/to/conventions.json

See `conventions.example.json` for the shape. Keeping the defaults here and
the personal version outside the repo is also what makes this safe to publish:
no real itinerary, address or booking reference is ever committed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .schema import (
    FlightRecord,
    LocalTime,
    LodgingRecord,
    OtherRecord,
    TrainRecord,
)
from .timeutil import format_local

DEFAULT_CONVENTIONS: dict[str, Any] = {
    "flight_title": "{origin} → {destination_city} ({carrier} {number})",
    "train_title": "{operator} {number} {origin} → {destination}",
    "lodging_title": "{property_name}",
    "other_title": "{title}",
    # "span" = one event from check-in to check-out.
    # "endpoints" = separate "Check in" and "Check out" events.
    "lodging_style": "span",
    "lodging_checkin_title": "Check in — {property_name}",
    "lodging_checkout_title": "Check out — {property_name}",
    # Prefix applied to everything, e.g. "✈ ". Empty by default.
    "title_prefix": "",
    # Include the booking reference in the event description.
    "include_confirmation": True,
    # Google Calendar colorId for events created in the pending calendar.
    "pending_color_id": "6",
    # Reminders, in minutes before the event starts. Two for flights: the
    # first is "leave now", the second is "you should already be at the desk".
    # A single late reminder is no use as the only warning, and a single early
    # one is easy to dismiss and forget.
    "flight_reminders_minutes": [180, 45],
    "train_reminders_minutes": [60, 15],
    "lodging_reminders_minutes": [],
    "other_reminders_minutes": [],
    # Google caps an event at five reminders and rejects anything over 40320
    # minutes (four weeks) ahead.
}


def load_conventions() -> dict[str, Any]:
    """Defaults, overridden by the user's file if there is one.

    Checked in the same order the rest of the configuration uses: the
    environment variable first, then the deployed location, so the CLI and the
    running service always render titles the same way.
    """
    from .config import USER_CONFIG_DIR

    conventions = dict(DEFAULT_CONVENTIONS)
    path = os.environ.get("WAYFARE_CONVENTIONS")
    if not path:
        deployed = USER_CONFIG_DIR / "conventions.json"
        path = str(deployed) if deployed.exists() else ""
    if path:
        try:
            conventions.update(json.loads(Path(path).expanduser().read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass  # A malformed override falls back to defaults rather than failing a write.
    return conventions


def fallback_timezone(conventions: dict[str, Any] | None = None) -> str:
    """The zone to write when a record's own zone could not be resolved.

    Google rejects a naive dateTime outright, so "unknown" cannot be sent as
    such — the choice is between some zone and no event at all. The record
    still carries its "no timezone" warning, so it is held for review either
    way; this only decides what the held event looks like on the calendar.
    """
    from .config import get_config

    c = conventions if conventions is not None else load_conventions()
    return c.get("default_timezone") or get_config().timezone


def start_local(record) -> LocalTime | None:
    for name in ("departure", "check_in", "start"):
        value = getattr(record, name, None)
        if value is not None:
            return value
    return None


def end_local(record) -> LocalTime | None:
    for name in ("arrival", "check_out", "end"):
        value = getattr(record, name, None)
        if value is not None:
            return value
    return None


def event_summary(record, conventions: dict[str, Any] | None = None) -> str:
    c = conventions or load_conventions()
    prefix = c.get("title_prefix", "")

    if isinstance(record, FlightRecord):
        title = c["flight_title"].format(
            carrier_number=record.flight_designator(),
            carrier=record.carrier or "",
            number=record.number or "",
            origin=record.origin.label(),
            destination=record.destination.label(),
            origin_city=record.origin.city or record.origin.label(),
            destination_city=record.destination.city or record.destination.label(),
        )
    elif isinstance(record, TrainRecord):
        # The same template serves rail, coach and ferry; {mode} is there for
        # anyone who wants the difference in the title.
        title = c["train_title"].format(
            operator=record.operator or {"bus": "Coach", "ferry": "Ferry"}.get(record.mode, "Train"),
            number=record.number or "",
            mode=record.mode,
            origin=record.origin.label(),
            destination=record.destination.label(),
            origin_city=record.origin.city or record.origin.label(),
            destination_city=record.destination.city or record.destination.label(),
        )
    elif isinstance(record, LodgingRecord):
        title = c["lodging_title"].format(
            property_name=record.property_name or record.location.label(),
            location=record.location.label(),
        )
    elif isinstance(record, OtherRecord):
        title = c["other_title"].format(title=record.title)
    else:  # pragma: no cover - the union is closed
        title = "Travel"

    return f"{prefix}{' '.join(title.split())}"


#: Google's own limits on the reminders field.
MAX_REMINDERS = 5
MAX_REMINDER_MINUTES = 40320


def event_reminders(record, conventions: dict[str, Any] | None = None) -> dict | None:
    """Reminder overrides for a record, or None to keep the calendar default.

    Two reminders on a flight rather than one: the early one is the cue to
    leave, the late one is the cue that you should already be at the desk.
    Both are configurable, and setting the list to empty restores whatever
    default the calendar itself uses.
    """
    c = conventions or load_conventions()
    key = {
        FlightRecord: "flight_reminders_minutes",
        TrainRecord: "train_reminders_minutes",
        LodgingRecord: "lodging_reminders_minutes",
    }.get(type(record), "other_reminders_minutes")

    raw = c.get(key)
    if raw is None:
        return None
    if raw == []:
        return {"useDefault": True}

    minutes: list[int] = []
    for value in raw:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= value <= MAX_REMINDER_MINUTES and value not in minutes:
            minutes.append(value)

    if not minutes:
        return {"useDefault": True}

    minutes.sort(reverse=True)
    return {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": m} for m in minutes[:MAX_REMINDERS]],
    }


def event_location(record) -> str | None:
    if isinstance(record, (FlightRecord, TrainRecord)):
        origin = record.origin
        if origin.address:
            return origin.address
        # The full name, never the code — this field has to work in Maps. The
        # hall is appended when there is one, because at a station that is the
        # part that tells you where to stand.
        if origin.name:
            return f"{origin.name}, {origin.detail}" if origin.detail else origin.name
        return origin.iata
    if isinstance(record, LodgingRecord):
        return record.location.address or record.location.name
    if isinstance(record, OtherRecord) and record.location:
        return record.location.address or record.location.name
    return None


def _endpoint(place) -> str:
    """A place as the description should show it: name, hall, then city."""
    parts = [place.label()]
    if place.detail and place.detail not in parts[0]:
        parts.append(place.detail)
    if place.city and place.city not in parts[0]:
        parts.append(place.city)
    return ", ".join(parts)


def event_description(record, conventions: dict[str, Any] | None = None) -> str:
    """A description that shows the booking facts and how they were verified."""
    c = conventions or load_conventions()
    lines: list[str] = []

    if isinstance(record, (FlightRecord, TrainRecord)):
        # The hall or concourse belongs here rather than in the title: it is
        # useless until you are at the station, and then it is the only thing
        # you want.
        lines.append(f"Depart: {format_local(record.departure)} — {_endpoint(record.origin)}")
        if record.arrival:
            lines.append(f"Arrive: {format_local(record.arrival)} — {_endpoint(record.destination)}")
        for label, attr in (
            ("Terminal", "terminal_departure"),
            ("Seat", "seat"),
            ("Coach", "coach"),
            ("Cabin", "cabin"),
        ):
            value = getattr(record, attr, None)
            if value:
                lines.append(f"{label}: {value}")
    elif isinstance(record, LodgingRecord):
        lines.append(f"Check in:  {format_local(record.check_in)}")
        lines.append(f"Check out: {format_local(record.check_out)}")
        if record.room:
            lines.append(f"Room: {record.room}")
        if record.location.address:
            lines.append(f"Address: {record.location.address}")
    elif isinstance(record, OtherRecord) and record.description:
        lines.append(record.description)

    if c.get("include_confirmation", True) and record.confirmation:
        lines.append(f"Confirmation: {record.confirmation}")
    if record.traveller:
        lines.append(f"Traveller: {record.traveller}")

    notes = [i for i in record.issues if i.level.value in {"warn", "error"}]
    if notes:
        lines.append("")
        lines.append("Needs checking:")
        lines.extend(f"  • {i.message}" for i in notes)

    lines.append("")
    lines.append(
        f"— wayfare ({record.provenance.describe()}, confidence {record.confidence():.0%})"
    )
    return "\n".join(lines)


def to_google_events(record, conventions: dict[str, Any] | None = None) -> list[dict]:
    """Render one record as one or more Google Calendar event bodies."""
    c = conventions or load_conventions()
    fallback = fallback_timezone(c)

    reminders = event_reminders(record, c)

    if isinstance(record, LodgingRecord) and c.get("lodging_style") == "endpoints":
        name = record.property_name or record.location.label()
        description = event_description(record, c)
        location = event_location(record)
        events = [
            {
                "summary": f"{c.get('title_prefix', '')}"
                + c["lodging_checkin_title"].format(property_name=name),
                "start": record.check_in.to_google(fallback),
                "end": record.check_in.to_google(fallback),
                "location": location,
                "description": description,
            },
            {
                "summary": f"{c.get('title_prefix', '')}"
                + c["lodging_checkout_title"].format(property_name=name),
                "start": record.check_out.to_google(fallback),
                "end": record.check_out.to_google(fallback),
                "location": location,
                "description": description,
            },
        ]
        if reminders is not None:
            for event in events:
                event["reminders"] = reminders
        return events

    start = start_local(record)
    end = end_local(record) or start
    if start is None:
        return []

    body = {
        "summary": event_summary(record, c),
        "start": start.to_google(fallback),
        "end": end.to_google(fallback),
        "description": event_description(record, c),
    }
    location = event_location(record)
    if location:
        body["location"] = location
    if reminders is not None:
        body["reminders"] = reminders
    return [body]
