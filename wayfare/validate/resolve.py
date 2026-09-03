"""Fill in what the airport database knows, so later validators can work."""

from __future__ import annotations

from ..airports import get_airport_db
from ..schema import (
    FlightRecord,
    Issue,
    IssueLevel,
    Itinerary,
    LocalTime,
    Place,
    TrainRecord,
)

SOURCE = "resolve"


def _resolve_place(place: Place | None) -> list[Issue]:
    """Attach coordinates and a timezone to a place identified by IATA code."""
    issues: list[Issue] = []
    if place is None or not place.iata:
        return issues

    db = get_airport_db()
    airport = db.get(place.iata)
    if airport is None:
        if db.available:
            issues.append(
                Issue(
                    level=IssueLevel.WARN,
                    code="place.unknown_iata",
                    message=(
                        f"'{place.iata}' is not a scheduled-service airport in the reference "
                        "database. It may be misread — O/0 and I/1 are the usual culprits."
                    ),
                    source=SOURCE,
                )
            )
        else:
            issues.append(
                Issue(
                    level=IssueLevel.INFO,
                    code="place.no_airport_db",
                    message="Airport database not installed; skipping code and timezone checks.",
                    source=SOURCE,
                )
            )
        return issues

    place.latitude = place.latitude if place.latitude is not None else airport.latitude
    place.longitude = place.longitude if place.longitude is not None else airport.longitude
    if not place.name:
        place.name = airport.name
    if not place.city:
        place.city = airport.city or None

    zone = db.timezone_for(place.iata)
    if zone and not place.timezone:
        place.timezone = zone
    elif zone and place.timezone and place.timezone != zone:
        issues.append(
            Issue(
                level=IssueLevel.WARN,
                code="place.timezone_conflict",
                message=(
                    f"{place.iata}: extracted timezone '{place.timezone}' disagrees with the "
                    f"airport's actual zone '{zone}'."
                ),
                source=SOURCE,
            )
        )
    return issues


def _resolve_by_name(place: Place | None) -> list[Issue]:
    """Give a non-airport place a timezone from the city it names.

    A hotel booking has an address and a city, never an IATA code. Without
    this, every stay carried a "no timezone" warning and was held for review
    for a reason the user could do nothing about.
    """
    issues: list[Issue] = []
    if place is None or place.timezone or place.iata:
        return issues

    db = get_airport_db()
    if not db.available:
        return issues

    airport = db.find_place(place.city, place.address, place.name)
    if airport is None:
        return issues

    zone = db.timezone_for(airport.iata)
    if not zone:
        return issues

    place.timezone = zone
    if not place.city:
        place.city = airport.city or None
    issues.append(
        Issue(
            level=IssueLevel.INFO,
            code="place.timezone_from_city",
            message=f"Timezone {zone} taken from {airport.city or airport.name}.",
            source=SOURCE,
        )
    )
    return issues


def _apply_zone(when: LocalTime | None, place: Place | None) -> None:
    """A ticket time is local to its endpoint unless it says otherwise."""
    if when is None or place is None:
        return
    if not when.timezone and place.timezone:
        when.timezone = place.timezone


def run(itinerary: Itinerary) -> Itinerary:
    for record in itinerary.records:
        places: list[tuple[Place | None, LocalTime | None]] = []

        if isinstance(record, (FlightRecord, TrainRecord)):
            places = [
                (record.origin, record.departure),
                (record.destination, record.arrival),
            ]
        elif hasattr(record, "location"):
            location = getattr(record, "location", None)
            places = [
                (location, getattr(record, "check_in", None)),
                (location, getattr(record, "check_out", None)),
                (location, getattr(record, "start", None)),
                (location, getattr(record, "end", None)),
            ]

        seen: set[int] = set()
        for place, when in places:
            if place is not None and id(place) not in seen:
                seen.add(id(place))
                record.issues.extend(_resolve_place(place))
                record.issues.extend(_resolve_by_name(place))
            _apply_zone(when, place)

        _flag_missing_zone(record)
    return itinerary


def _flag_missing_zone(record) -> None:
    """A time with no zone is a time we cannot check. Say so once per record."""
    times = [
        getattr(record, name, None)
        for name in ("departure", "arrival", "check_in", "check_out", "start", "end")
    ]
    if any(t is not None and not t.timezone for t in times):
        record.add_issue(
            IssueLevel.WARN,
            "time.no_timezone",
            "At least one time has no resolved timezone, so it will be created in the "
            "calendar's default zone. Confirm the hour before trusting it.",
            SOURCE,
        )
