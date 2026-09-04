"""Do the records agree with each other, and with the calendar already there?

Single-record checks catch garbled text. These catch the errors that survive
them: a hotel booked for the wrong month, a return flight parsed with this
year's date instead of next year's, a connection that cannot be made, the same
booking submitted twice.

Everything here is pure logic over already-validated records, so it needs no
network and no key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..schema import (
    FlightRecord,
    IssueLevel,
    Itinerary,
    LodgingRecord,
    TrainRecord,
)
from ..timeutil import format_local, to_utc

SOURCE = "coherence"

#: Below this, a connection is not realistically makeable at a large airport.
TIGHT_CONNECTION_MIN = 45
#: Above this, the two legs are separate trips rather than a connection.
STOPOVER_HOURS = 24
#: A hotel stay longer than this is almost certainly a misread year.
LONG_STAY_DAYS = 60


def _departure_order(record) -> datetime:
    """A sort key that is always comparable with the others.

    `to_utc` returns an aware datetime when the leg's zone was resolved and
    None when it was not, and the fallback used to hand back a naive one.
    Python refuses to compare the two, so an itinerary holding one leg with a
    known zone and one without did not merely sort oddly — it raised, out of a
    validator, and lost the whole document. Measured on the corpus: four of
    six flight itineraries crashed here, which is most of what "could not be
    read at all" meant.

    A leg with no zone is ordered by its local clock read as though it were
    UTC. That is wrong by at most a day, and only for ordering; every check
    that cares about the actual instant asks `to_utc` for itself and skips the
    leg when it gets None.
    """
    stamp = to_utc(record.departure)
    if stamp is not None:
        return stamp
    return record.departure.local.replace(tzinfo=timezone.utc)


def _legs(itinerary: Itinerary) -> list:
    """Flights and trains together, in departure order."""
    legs = [r for r in itinerary.records if isinstance(r, (FlightRecord, TrainRecord))]
    return sorted(legs, key=_departure_order)


def _check_lodging(itinerary: Itinerary) -> None:
    for stay in itinerary.lodgings():
        check_in, check_out = to_utc(stay.check_in), to_utc(stay.check_out)
        if check_in is None or check_out is None:
            continue

        if check_out <= check_in:
            stay.add_issue(
                IssueLevel.ERROR,
                "lodging.checkout_before_checkin",
                f"Check-out ({format_local(stay.check_out)}) is not after check-in "
                f"({format_local(stay.check_in)}).",
                SOURCE,
            )
            continue

        nights = (check_out - check_in).days
        if nights > LONG_STAY_DAYS:
            stay.add_issue(
                IssueLevel.WARN,
                "lodging.stay_implausibly_long",
                f"A {nights}-night stay is unusual — check the year on both dates.",
                SOURCE,
            )


def _check_overlaps(itinerary: Itinerary) -> None:
    stays = [s for s in itinerary.lodgings() if to_utc(s.check_in) and to_utc(s.check_out)]
    stays.sort(key=lambda s: to_utc(s.check_in))
    for earlier, later in zip(stays, stays[1:]):
        if to_utc(later.check_in) < to_utc(earlier.check_out):
            later.add_issue(
                IssueLevel.WARN,
                "lodging.overlapping_stays",
                f"This stay starts before '{earlier.property_name or 'the previous stay'}' "
                "ends. One of the two has the wrong dates, unless you really booked both.",
                SOURCE,
            )


def _check_connections(itinerary: Itinerary) -> None:
    legs = _legs(itinerary)
    for first, second in zip(legs, legs[1:]):
        arrival = to_utc(getattr(first, "arrival", None))
        departure = to_utc(second.departure)
        if arrival is None or departure is None:
            continue

        gap = departure - arrival
        gap_minutes = gap.total_seconds() / 60

        if gap_minutes < 0:
            second.add_issue(
                IssueLevel.ERROR,
                "leg.departs_before_previous_arrival",
                f"This leg departs {abs(gap_minutes):.0f} min before the previous leg lands. "
                "The two cannot both be right.",
                SOURCE,
            )
            continue

        if gap_minutes < TIGHT_CONNECTION_MIN:
            second.add_issue(
                IssueLevel.WARN,
                "leg.connection_too_tight",
                f"Only {gap_minutes:.0f} min between legs. Either the connection is genuinely "
                "very tight, or one of the times is wrong.",
                SOURCE,
            )

        if gap_minutes / 60 < STOPOVER_HOURS:
            previous_end = getattr(first, "destination", None)
            next_start = getattr(second, "origin", None)
            if (
                previous_end is not None
                and next_start is not None
                and previous_end.iata
                and next_start.iata
                and previous_end.iata.upper() != next_start.iata.upper()
            ):
                second.add_issue(
                    IssueLevel.WARN,
                    "leg.connection_airport_mismatch",
                    f"Previous leg lands at {previous_end.iata} but this one departs from "
                    f"{next_start.iata}, {gap_minutes / 60:.1f}h later. Confirm the ground "
                    "transfer, or one of the codes is misread.",
                    SOURCE,
                )


def _check_lodging_against_travel(itinerary: Itinerary) -> None:
    """The check that pays for the whole tool: hotel dates versus flight dates."""
    legs = _legs(itinerary)
    if not legs:
        return

    arrivals = [to_utc(getattr(leg, "arrival", None)) for leg in legs]
    arrivals = [a for a in arrivals if a is not None]
    departures = [to_utc(leg.departure) for leg in legs]
    departures = [d for d in departures if d is not None]
    if not arrivals or not departures:
        return

    first_arrival = min(arrivals)

    for stay in itinerary.lodgings():
        check_in, check_out = to_utc(stay.check_in), to_utc(stay.check_out)
        if check_in is None or check_out is None:
            continue

        if check_in < first_arrival - timedelta(hours=6):
            stay.add_issue(
                IssueLevel.WARN,
                "lodging.checkin_before_arrival",
                f"Check-in ({format_local(stay.check_in)}) is before you land on the first "
                "leg of this itinerary. Check the date.",
                SOURCE,
            )

        # Only a leg that leaves *after* you check in can be the journey home.
        # Comparing against the outbound flight flagged every one-way trip and
        # every booking submitted before the return was arranged.
        onward = [d for d in departures if d > check_in]
        if onward and check_out > max(onward) + timedelta(hours=6):
            stay.add_issue(
                IssueLevel.WARN,
                "lodging.checkout_after_departure",
                f"Check-out ({format_local(stay.check_out)}) is after your last departure "
                "from this trip. Check the date.",
                SOURCE,
            )


def _check_duplicates(itinerary: Itinerary, existing_events: list) -> None:
    """Warn when a record looks like something already on the calendar.

    Deliberately a warning and never a skip: two genuinely separate legs on the
    same route and day do happen, and silently dropping one is worse than
    asking.
    """
    if not existing_events:
        return

    index = {
        (str(e.get("summary", "")).strip().lower(), str(e.get("start_date", "")))
        for e in existing_events
    }

    for record in itinerary.records:
        from ..render import event_summary, start_local

        when = start_local(record)
        if when is None:
            continue
        key = (event_summary(record).strip().lower(), when.local.date().isoformat())
        if key in index:
            record.add_issue(
                IssueLevel.WARN,
                "record.possible_duplicate",
                f"'{event_summary(record)}' already appears on the calendar that day. "
                "This may be a re-submission of a booking you already added.",
                SOURCE,
            )


def run(itinerary: Itinerary, existing_events: list | None = None) -> Itinerary:
    _check_lodging(itinerary)
    _check_overlaps(itinerary)
    _check_connections(itinerary)
    _check_lodging_against_travel(itinerary)
    _check_duplicates(itinerary, existing_events or [])
    return itinerary
