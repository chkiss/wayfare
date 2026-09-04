"""A check that fails must not take the reading down with it.

Measured on the corpus: four of six flight itineraries were read correctly and
then thrown away by a validator, and reported to the user as "could not be
read at all". The reading was fine. The code checking it raised.
"""

from datetime import datetime

import pytest

from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
)
from wayfare.validate import coherence, run_all


def leg(number, hour, zone):
    return FlightRecord(
        carrier="DL",
        number=number,
        origin=Place(iata="LHR", city="London"),
        destination=Place(iata="JFK", city="New York"),
        departure=LocalTime(local=datetime(2026, 9, 27, hour, 0), timezone=zone),
        provenance=Provenance(extractor="llm"),
    )


def test_a_leg_with_no_timezone_beside_one_with_a_zone_does_not_raise():
    """The measured crash: the sort key was aware for one leg and naive for
    the other, and Python will not compare them."""
    itinerary = Itinerary(records=[leg("273", 9, "Europe/London"), leg("274", 18, None)])
    ordered = coherence._legs(itinerary)
    assert [r.number for r in ordered] == ["273", "274"]


def test_the_order_is_still_right_across_zones():
    later_but_earlier_clock = leg("274", 8, "America/New_York")  # 13:00 UTC
    itinerary = Itinerary(records=[later_but_earlier_clock, leg("273", 9, "Europe/London")])
    assert [r.number for r in coherence._legs(itinerary)] == ["273", "274"]


def test_a_validator_that_raises_loses_only_its_own_findings(monkeypatch):
    def explode(*args, **kwargs):
        raise TypeError("can't compare offset-naive and offset-aware datetimes")

    monkeypatch.setattr(coherence, "run", explode)
    itinerary = Itinerary(records=[leg("273", 9, "Europe/London")])

    result = run_all(itinerary)

    assert len(result.records) == 1, "the reading survived the failed check"
    (issue,) = [i for i in result.issues if i.code == "validate.check_failed"]
    assert "coherence" in issue.message


def test_the_remaining_checks_still_run_after_one_fails(monkeypatch):
    from wayfare.validate import geo

    monkeypatch.setattr(geo, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    itinerary = Itinerary(records=[leg("273", 9, "Europe/London")])
    result = run_all(itinerary)

    # resolve ran before it, coherence and completeness after it.
    assert result.records[0].origin.timezone == "Europe/London"
    assert [i.code for i in result.issues].count("validate.check_failed") == 1
