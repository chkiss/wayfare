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


#: Words that describe a station rather than name a place. Stripping them is
#: what turns "Boston South Station" into something the city index can match.
#: Rail stations are the case that needs this: an airport carries an IATA code,
#: a hotel carries an address, a station carries neither.
STATION_WORDS = {
    "station",
    "stn",
    "rail",
    "railway",
    "train",
    "terminal",
    "terminus",
    "bus",
    "coach",
    "ferry",
    "port",
    "pier",
    "gare",
    "bahnhof",
    "hbf",
    "hauptbahnhof",
    "centraal",
    "central",
    "centrale",
    "estacion",
    "estación",
    "stazione",
    "st",
    "amtrak",
    "via",
}

#: Connectors that begin no city name. Without these, "Gare de Lyon" reduces
#: to "de Lyon" and matches nothing.
_CONNECTORS = {"de", "du", "des", "la", "le", "les", "of", "the", "den", "van", "di", "el"}


def _city_guesses(place: Place) -> list[str]:
    """Candidate city names hidden inside a station name.

    "Back Bay Station" yields "Back Bay"; "Boston South Station" yields
    "Boston South" and "Boston". Tried in order, longest first, so a specific
    match wins before a one-word guess that might collide.
    """
    if not place.name:
        return []

    # "Boston, MA" and "Paris (Gare de Lyon)" both put the city first.
    head = place.name.split(",")[0].split("(")[0]
    words = [w for w in head.replace("-", " ").split() if w]
    kept = [w for w in words if w.strip(".").lower() not in STATION_WORDS]
    while kept and kept[0].strip(".").lower() in _CONNECTORS:
        kept.pop(0)

    guesses: list[str] = []
    for length in range(len(kept), 0, -1):
        candidate = " ".join(kept[:length]).strip()
        if len(candidate) > 2 and candidate not in guesses:
            guesses.append(candidate)
    return guesses


def _resolve_by_name(place: Place | None) -> list[Issue]:
    """Give a non-airport place a timezone from the city it names.

    A hotel booking has an address and a city, never an IATA code. Without
    this, every stay carried a "no timezone" warning and was held for review
    for a reason the user could do nothing about.
    """
    issues: list[Issue] = []
    if place is None or place.timezone or place.iata:
        return issues

    # A station table answers this outright. Guessing a city from a station
    # name and then an airport from the city is two inferences deep — "Gare de
    # Lyon is in Paris" is the easy case and the one the guesswork was built
    # for; "MONTPELLIER ST-RO" is not. The station's own row carries its
    # timezone, its coordinates and a UIC code, so where it is known nothing
    # has to be inferred at all.
    from ..reference import station as lookup_station

    found = lookup_station(place.name)
    if found is not None and found.timezone:
        place.timezone = found.timezone
        if found.latitude is not None and place.latitude is None:
            place.latitude, place.longitude = found.latitude, found.longitude
        issues.append(
            Issue(
                level=IssueLevel.INFO,
                code="place.station_resolved",
                message=(
                    f"'{place.name}' is {found.name}"
                    + (f" (UIC {found.uic})" if found.uic else "")
                    + f", timezone {found.timezone}."
                ),
                source=SOURCE,
            )
        )
        return issues

    db = get_airport_db()
    if not db.available:
        return issues

    # A city or an address names the place itself. A station name only contains
    # it, and sometimes contains the wrong one — Gare de Lyon is in Paris — so
    # a match from that route is trusted for the timezone and nothing else.
    named = db.find_place(place.city, place.address)
    airport = named or db.find_place(place.name, *_city_guesses(place))
    if airport is None:
        return issues

    zone = db.timezone_for(airport.iata)
    if not zone:
        return issues

    place.timezone = zone
    if named is not None and not place.city:
        place.city = airport.city or None

    # Approximate coordinates too, so a surface leg between two stations can be
    # checked against a distance. Accurate to the city, which is the scale the
    # speed bands work at — they would not survive being trusted for more.
    if place.latitude is None and place.longitude is None:
        place.latitude, place.longitude = airport.latitude, airport.longitude
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
        _flag_unnamed_endpoint(record)
        _flag_missing_service_number(record)
    return itinerary


def _flag_missing_service_number(record) -> None:
    """A leg with no service number is not identifiable, and looks it.

    Held rather than written, because a flight number is how you find the leg
    again at the airport, and its absence usually means the reading was partial
    rather than that the ticket omitted it.
    """
    if not isinstance(record, (FlightRecord, TrainRecord)):
        return
    if getattr(record, "number", None):
        return
    operator = getattr(record, "carrier", None) or getattr(record, "operator", None)
    if not operator:
        return  # Neither read: already reported as an unnamed journey.
    record.add_issue(
        IssueLevel.WARN,
        "leg.no_service_number",
        f"The {operator} service number was not read from the document. Add it before "
        "adding this to your calendar, or you will not be able to find the leg again.",
        SOURCE,
    )


def _flag_unnamed_endpoint(record) -> None:
    """An endpoint that was never read leaves a "→ ?" on the calendar.

    Worth saying in its own words. The reader can tell from the title that
    something is missing, but not that the tool knows it is missing, nor which
    end of the journey it was.
    """
    if not isinstance(record, (FlightRecord, TrainRecord)):
        return
    for label, place in (("origin", record.origin), ("destination", record.destination)):
        if place.label() == "?":
            record.add_issue(
                IssueLevel.WARN,
                f"leg.{label}_not_read",
                f"The {label} could not be read from the document, so the title shows "
                "'?'. Type it in before adding this to your calendar.",
                SOURCE,
            )


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
