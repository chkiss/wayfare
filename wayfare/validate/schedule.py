"""Optional check: does that flight number actually fly at that time?

This is the only validator that needs the network, so it is strictly an
*upgrade*. It can turn "plausible" into "confirmed" and it can catch a
correctly-formed but wrong flight number, but the tool is fully functional
with `WAYFARE_SCHEDULE_PROVIDER=none` (the default).

A provider failure is reported as INFO, never as a warning: an expired key or
a rate limit is a fact about the API, not evidence about the itinerary, and it
must never be allowed to hold up an otherwise clean event.
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from ..config import get_config
from ..schema import FlightRecord, IssueLevel, Itinerary
from ..timeutil import format_local

SOURCE = "schedule"

#: How far the scheduled departure may differ from the extracted one before
#: it counts as a disagreement rather than a schedule change.
TOLERANCE_MIN = 30


def _lookup_aerodatabox(designator: str, date: str, api_key: str) -> list[dict]:
    url = f"https://aerodatabox.p.rapidapi.com/flights/number/{designator}/{date}"
    response = httpx.get(
        url,
        headers={
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": "aerodatabox.p.rapidapi.com",
        },
        params={"withAircraftImage": "false", "withLocation": "false"},
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else [payload]


def _scheduled_departures(entries: list[dict]) -> list[tuple[str, str]]:
    """Extract (local departure time, origin IATA) pairs from a provider reply."""
    out: list[tuple[str, str]] = []
    for entry in entries:
        departure = entry.get("departure") or {}
        scheduled = (departure.get("scheduledTime") or {}).get("local")
        airport = (departure.get("airport") or {}).get("iata")
        if scheduled:
            out.append((scheduled, airport or ""))
    return out


def _check_flight(flight: FlightRecord, provider: str, api_key: str) -> None:
    designator = flight.flight_designator()
    if not (flight.carrier and flight.number):
        flight.add_issue(
            IssueLevel.INFO,
            "flight.no_designator",
            "No flight number was extracted, so the published schedule was not checked.",
            SOURCE,
        )
        return

    date = flight.departure.local.date().isoformat()
    try:
        if provider == "aerodatabox":
            entries = _lookup_aerodatabox(designator, date, api_key)
        else:
            flight.add_issue(
                IssueLevel.INFO,
                "schedule.unknown_provider",
                f"Schedule provider '{provider}' is not implemented; check skipped.",
                SOURCE,
            )
            return
    except Exception as exc:  # noqa: BLE001 - any provider failure is non-fatal
        flight.add_issue(
            IssueLevel.INFO,
            "schedule.lookup_failed",
            f"Could not reach the schedule provider ({type(exc).__name__}); check skipped.",
            SOURCE,
        )
        return

    departures = _scheduled_departures(entries)
    if not departures:
        flight.add_issue(
            IssueLevel.WARN,
            "flight.not_in_schedule",
            f"{designator} does not appear in the published schedule for {date}. "
            "The flight number or the date may be misread.",
            SOURCE,
        )
        return

    extracted = flight.departure.local
    for scheduled_text, origin_iata in departures:
        try:
            scheduled = _parse_local(scheduled_text)
        except ValueError:
            continue
        if abs(scheduled - extracted) <= timedelta(minutes=TOLERANCE_MIN):
            if (
                origin_iata
                and flight.origin.iata
                and origin_iata.upper() != flight.origin.iata.upper()
            ):
                flight.add_issue(
                    IssueLevel.WARN,
                    "flight.origin_mismatch",
                    f"{designator} is scheduled from {origin_iata}, not "
                    f"{flight.origin.iata}.",
                    SOURCE,
                )
                return
            flight.add_issue(
                IssueLevel.INFO,
                "flight.schedule_confirmed",
                f"{designator} confirmed against the published schedule for {date}.",
                SOURCE,
            )
            return

    listed = ", ".join(text for text, _ in departures[:3])
    flight.add_issue(
        IssueLevel.WARN,
        "flight.schedule_time_mismatch",
        f"{designator} on {date} is scheduled at {listed}, but the document was read as "
        f"{format_local(flight.departure)}. Check the departure time.",
        SOURCE,
    )


def _parse_local(text: str):
    """Parse the provider's local-time string, which has no zone suffix."""
    from datetime import datetime

    cleaned = text.strip().replace("Z", "").split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised schedule time: {text!r}")


def run(itinerary: Itinerary) -> Itinerary:
    cfg = get_config()
    provider = (cfg.schedule_provider or "none").strip().lower()
    if provider in {"", "none", "off", "disabled"}:
        return itinerary

    api_key = cfg.schedule_api_key
    if not api_key:
        for flight in itinerary.flights():
            flight.add_issue(
                IssueLevel.INFO,
                "schedule.no_api_key",
                f"Schedule provider '{provider}' is configured but no API key is present.",
                SOURCE,
            )
        return itinerary

    for flight in itinerary.flights():
        _check_flight(flight, provider, api_key)
    return itinerary
