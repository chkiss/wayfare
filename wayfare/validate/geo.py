"""Is the journey physically possible?

This is the cheapest high-value check in the tool. A flight's printed
departure and arrival are local times at two different airports; once both
zones are known, the real elapsed time is fixed, and it has to be consistent
with the distance actually being flown.

It catches the errors OCR actually makes: a dropped or invented digit in the
hour, a swapped am/pm, a mis-set timezone, and origin/destination read in the
wrong order.
"""

from __future__ import annotations

from ..airports import distance_between, great_circle_km
from ..schema import FlightRecord, IssueLevel, Itinerary, TrainRecord
from ..timeutil import elapsed_hours

SOURCE = "geo"

#: Time on the ground at each end that is not spent flying: pushback, taxi,
#: departure queue, taxi-in, gate arrival.
GROUND_OVERHEAD_H = 0.55

#: Plausible average airborne ground speeds, km/h. The wide band absorbs
#: headwinds, holding patterns, and the poor speed economics of short hops.
FAST_KMH = 950.0
SLOW_KMH = 620.0

#: Surface transport is slower and far more variable than flying, so only
#: gross errors are flagged. Per mode, because a coach doing 300 km/h and a
#: ferry doing 120 are both impossible while a train doing either is not.
#: (fastest km/h, slowest km/h, hours of overhead, noun for the message)
SURFACE_BOUNDS = {
    "train": (300.0, 45.0, 0.3, "rail"),
    "bus": (110.0, 25.0, 0.4, "coach"),
    "ferry": (75.0, 15.0, 0.75, "ferry"),
}

#: Kept as names because the rail figures are referred to directly in tests
#: and by callers that predate the per-mode table.
RAIL_FAST_KMH = SURFACE_BOUNDS["train"][0]
RAIL_SLOW_KMH = SURFACE_BOUNDS["train"][1]


def _has_position(place) -> bool:
    return place.latitude is not None and place.longitude is not None


def _bounds(distance_km: float, fast: float, slow: float, overhead: float) -> tuple[float, float]:
    """Minimum and maximum believable duration in hours for a given distance."""
    low = 0.25 + distance_km / fast
    high = overhead + 1.0 + distance_km / slow
    return low, high


def _check_leg(record, fast: float, slow: float, overhead: float, noun: str) -> None:
    departure = getattr(record, "departure", None)
    arrival = getattr(record, "arrival", None)
    if arrival is None:
        record.add_issue(
            IssueLevel.INFO,
            "leg.no_arrival",
            f"No arrival time was extracted, so the {noun} duration could not be checked.",
            SOURCE,
        )
        return

    hours = elapsed_hours(departure, arrival)
    if hours is None:
        record.add_issue(
            IssueLevel.INFO,
            "leg.duration_unchecked",
            "Timezones are unresolved, so the duration could not be checked.",
            SOURCE,
        )
        return

    if hours <= 0:
        record.add_issue(
            IssueLevel.ERROR,
            "leg.arrival_before_departure",
            f"Arrival is {abs(hours):.1f}h before departure once timezones are applied. "
            "The times, the dates or the direction of travel were read wrongly.",
            SOURCE,
        )
        return

    distance = None
    origin, destination = getattr(record, "origin", None), getattr(record, "destination", None)
    if origin is not None and destination is not None:
        if origin.iata and destination.iata:
            if origin.iata.upper() == destination.iata.upper():
                record.add_issue(
                    IssueLevel.ERROR,
                    "leg.same_endpoints",
                    f"Origin and destination are both {origin.iata}.",
                    SOURCE,
                )
                return
            distance = distance_between(origin.iata, destination.iata)
        elif _has_position(origin) and _has_position(destination):
            # A station carries no IATA code, so without this no surface leg
            # was ever checked against a distance at all. The coordinates come
            # from the nearest airport to each endpoint: tens of kilometres out
            # on a journey of hundreds, which the speed band already absorbs.
            distance = great_circle_km(
                origin.latitude, origin.longitude, destination.latitude, destination.longitude
            )

    if distance is None:
        if hours > 20:
            record.add_issue(
                IssueLevel.WARN,
                "leg.duration_extreme",
                f"A {hours:.1f}h {noun} leg is unusually long and could not be checked "
                "against a distance.",
                SOURCE,
            )
        return

    low, high = _bounds(distance, fast, slow, overhead)

    if hours < low:
        record.add_issue(
            IssueLevel.ERROR,
            "leg.faster_than_possible",
            f"{distance:.0f} km in {hours:.1f}h is faster than {noun} travels "
            f"(minimum ≈ {low:.1f}h). A time or a timezone is wrong.",
            SOURCE,
        )
    elif hours > high * 2.0:
        record.add_issue(
            IssueLevel.ERROR,
            "leg.duration_impossible",
            f"{hours:.1f}h for {distance:.0f} km is far beyond any routing "
            f"(expected under ≈ {high:.1f}h). Likely a wrong date or a swapped digit.",
            SOURCE,
        )
    elif hours > high:
        record.add_issue(
            IssueLevel.WARN,
            "leg.block_time_implausible",
            f"{hours:.1f}h for {distance:.0f} km is longer than expected "
            f"(≈ {low:.1f}–{high:.1f}h). Check the arrival time and date.",
            SOURCE,
        )
    else:
        record.add_issue(
            IssueLevel.INFO,
            "leg.block_time_ok",
            f"{distance:.0f} km in {hours:.1f}h is consistent (expected {low:.1f}–{high:.1f}h).",
            SOURCE,
        )


def run(itinerary: Itinerary) -> Itinerary:
    for record in itinerary.records:
        if isinstance(record, FlightRecord):
            _check_leg(record, FAST_KMH, SLOW_KMH, GROUND_OVERHEAD_H, "flight")
        elif isinstance(record, TrainRecord):
            fast, slow, overhead, noun = SURFACE_BOUNDS.get(
                getattr(record, "mode", "train"), SURFACE_BOUNDS["train"]
            )
            _check_leg(record, fast, slow, overhead, noun)
    return itinerary
