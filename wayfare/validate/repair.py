"""Deterministic repairs for the known-ambiguous cases.

Only one repair is safe enough to apply automatically, and it is the one that
comes up constantly: a boarding pass or a screenshot that prints the arrival
*time* but not the arrival *date*. An overnight flight then looks like it
lands before it took off.

The fix is only applied when moving the arrival forward by whole days turns an
impossible leg into a plausible one, and it is always recorded as an issue so
the review screen shows that the tool moved the date itself.
"""

from __future__ import annotations

from ..airports import distance_between
from ..schema import FlightRecord, IssueLevel, Itinerary, TrainRecord
from ..timeutil import elapsed_hours, shift_days
from .geo import (
    FAST_KMH,
    GROUND_OVERHEAD_H,
    RAIL_FAST_KMH,
    RAIL_SLOW_KMH,
    SLOW_KMH,
    _bounds,
)

SOURCE = "repair"

#: Never roll a date forward by more than this. Two days covers the worst
#: real case (a westbound long-haul crossing the date line); more than that
#: means the record is wrong in a way a date shift cannot fix.
MAX_SHIFT_DAYS = 2


def _plausible(hours: float | None, distance_km: float | None, fast: float, slow: float) -> bool:
    if hours is None or hours <= 0:
        return False
    if distance_km is None:
        return hours < 20
    low, high = _bounds(distance_km, fast, slow, GROUND_OVERHEAD_H)
    return low <= hours <= high


def _repair_leg(record, fast: float, slow: float) -> None:
    departure = getattr(record, "departure", None)
    arrival = getattr(record, "arrival", None)
    if departure is None or arrival is None:
        return

    hours = elapsed_hours(departure, arrival)
    if hours is None or hours > 0:
        return  # Nothing to repair, or not enough information to try.

    distance = None
    origin, destination = getattr(record, "origin", None), getattr(record, "destination", None)
    if origin is not None and destination is not None and origin.iata and destination.iata:
        distance = distance_between(origin.iata, destination.iata)

    for days in range(1, MAX_SHIFT_DAYS + 1):
        candidate = shift_days(arrival, days)
        candidate_hours = elapsed_hours(departure, candidate)
        if _plausible(candidate_hours, distance, fast, slow):
            original = arrival.local.date().isoformat()
            setattr(record, "arrival", candidate)
            record.add_issue(
                IssueLevel.INFO,
                "leg.arrival_date_rolled",
                f"Arrival date moved from {original} to "
                f"{candidate.local.date().isoformat()} (+{days}d): the document gave an "
                "arrival time with no date, and the leg runs overnight.",
                SOURCE,
            )
            return

    record.add_issue(
        IssueLevel.WARN,
        "leg.arrival_unrepairable",
        "Arrival appears to precede departure and no whole-day shift makes the leg "
        "plausible. The times need checking by hand.",
        SOURCE,
    )


def run(itinerary: Itinerary) -> Itinerary:
    for record in itinerary.records:
        if isinstance(record, FlightRecord):
            _repair_leg(record, FAST_KMH, SLOW_KMH)
        elif isinstance(record, TrainRecord):
            _repair_leg(record, RAIL_FAST_KMH, RAIL_SLOW_KMH)
    return itinerary
