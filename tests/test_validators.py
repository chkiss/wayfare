from datetime import datetime

import pytest

from wayfare.schema import (
    FlightRecord,
    IssueLevel,
    Itinerary,
    LocalTime,
    LodgingRecord,
    Place,
    Provenance,
)
from wayfare.validate import coherence, geo, repair


def provenance():
    return Provenance(extractor="llm", source_file="test")


def flight(dep, arr, origin="LHR", destination="JFK", otz="Europe/London", dtz="America/New_York"):
    return FlightRecord(
        carrier="BA",
        number="117",
        origin=Place(iata=origin, timezone=otz),
        destination=Place(iata=destination, timezone=dtz),
        departure=LocalTime(local=datetime.fromisoformat(dep), timezone=otz),
        arrival=LocalTime(local=datetime.fromisoformat(arr), timezone=dtz),
        extraction_confidence=0.8,
        provenance=provenance(),
    )


def codes(record):
    return {issue.code for issue in record.issues}


# --- geo -----------------------------------------------------------------


def test_transatlantic_daytime_flight_is_accepted():
    """LHR-JFK leaves at 09:35 and lands 12:25 local — 8h50 in the air."""
    record = flight("2026-03-04T09:35", "2026-03-04T12:25")
    geo.run(Itinerary(records=[record]))
    assert "leg.block_time_ok" in codes(record)
    assert not record.errors


def test_arrival_before_departure_is_an_error():
    record = flight("2026-03-04T18:00", "2026-03-04T09:00")
    geo.run(Itinerary(records=[record]))
    assert "leg.arrival_before_departure" in codes(record)
    assert record.confidence() == 0.0


def test_misread_hour_is_caught_as_physically_impossible():
    """OCR turning the 12:25 arrival into 08:25 leaves 3h50 to cross the Atlantic."""
    record = flight("2026-03-04T09:35", "2026-03-04T08:25")
    geo.run(Itinerary(records=[record]))
    assert "leg.faster_than_possible" in codes(record)


def test_wrong_timezone_shows_up_as_an_impossible_duration():
    """Treating the New York arrival as London time hides five hours."""
    record = flight(
        "2026-03-04T09:35", "2026-03-04T12:25", dtz="Europe/London"
    )
    record.destination.timezone = "Europe/London"
    geo.run(Itinerary(records=[record]))
    assert "leg.faster_than_possible" in codes(record)


def test_same_origin_and_destination_is_an_error():
    record = flight("2026-03-04T09:35", "2026-03-04T12:25", destination="LHR", dtz="Europe/London")
    geo.run(Itinerary(records=[record]))
    assert "leg.same_endpoints" in codes(record)


# --- repair --------------------------------------------------------------


def test_overnight_flight_with_no_arrival_date_is_rolled_forward():
    """JFK 21:00 lands LHR 09:00 — the ticket printed no arrival date."""
    record = flight(
        "2026-03-04T21:00",
        "2026-03-04T09:00",
        origin="JFK",
        destination="LHR",
        otz="America/New_York",
        dtz="Europe/London",
    )
    repair.run(Itinerary(records=[record]))
    assert record.arrival.local.date().isoformat() == "2026-03-05"
    assert "leg.arrival_date_rolled" in codes(record)
    assert not record.errors


def test_unrepairable_leg_is_flagged_rather_than_guessed():
    record = flight("2026-03-04T09:00", "2026-03-01T09:00")
    repair.run(Itinerary(records=[record]))
    assert "leg.arrival_unrepairable" in codes(record)


# --- coherence -----------------------------------------------------------


def lodging(check_in, check_out, name="Hotel Example"):
    return LodgingRecord(
        property_name=name,
        location=Place(name=name, timezone="America/New_York"),
        check_in=LocalTime(local=datetime.fromisoformat(check_in), timezone="America/New_York"),
        check_out=LocalTime(local=datetime.fromisoformat(check_out), timezone="America/New_York"),
        extraction_confidence=0.8,
        provenance=provenance(),
    )


def test_hotel_checkin_before_the_flight_lands_is_flagged():
    itinerary = Itinerary(
        records=[
            flight("2026-03-04T09:35", "2026-03-04T12:25"),
            lodging("2026-03-01T15:00", "2026-03-08T11:00"),
        ]
    )
    coherence.run(itinerary)
    assert "lodging.checkin_before_arrival" in codes(itinerary.lodgings()[0])


def test_checkout_before_checkin_is_an_error():
    itinerary = Itinerary(records=[lodging("2026-03-08T15:00", "2026-03-04T11:00")])
    coherence.run(itinerary)
    assert "lodging.checkout_before_checkin" in codes(itinerary.lodgings()[0])


def test_misread_year_shows_up_as_an_implausibly_long_stay():
    itinerary = Itinerary(records=[lodging("2026-03-04T15:00", "2027-03-08T11:00")])
    coherence.run(itinerary)
    assert "lodging.stay_implausibly_long" in codes(itinerary.lodgings()[0])


def test_impossible_connection_is_an_error():
    first = flight("2026-03-04T09:35", "2026-03-04T12:25")
    second = flight(
        "2026-03-04T11:00",
        "2026-03-04T14:00",
        origin="JFK",
        destination="ORD",
        otz="America/New_York",
        dtz="America/Chicago",
    )
    coherence.run(Itinerary(records=[first, second]))
    assert "leg.departs_before_previous_arrival" in codes(second)


def test_tight_connection_is_a_warning_not_an_error():
    first = flight("2026-03-04T09:35", "2026-03-04T12:25")
    second = flight(
        "2026-03-04T12:50",
        "2026-03-04T14:30",
        origin="JFK",
        destination="ORD",
        otz="America/New_York",
        dtz="America/Chicago",
    )
    coherence.run(Itinerary(records=[first, second]))
    assert "leg.connection_too_tight" in codes(second)
    assert not second.errors


def test_duplicate_against_the_existing_calendar_is_warned():
    record = flight("2026-03-04T09:35", "2026-03-04T12:25")
    from wayfare.render import event_summary

    existing = [{"summary": event_summary(record), "start_date": "2026-03-04"}]
    coherence.run(Itinerary(records=[record]), existing)
    assert "record.possible_duplicate" in codes(record)


# --- lodging timezones ---------------------------------------------------


def test_a_hotel_gets_a_timezone_from_its_city():
    """A booking gives a city and a street, never an IATA code."""
    from wayfare.validate import resolve

    stay = LodgingRecord(
        property_name="Some Hotel",
        location=Place(name="Some Hotel", city="New York", address="12 W 44th St, New York"),
        check_in=LocalTime(local=datetime(2026, 3, 4, 15, 0)),
        check_out=LocalTime(local=datetime(2026, 3, 8, 11, 0)),
        extraction_confidence=0.8,
        provenance=provenance(),
    )
    resolve.run(Itinerary(records=[stay]))
    assert stay.location.timezone == "America/New_York"
    assert stay.check_in.timezone == "America/New_York"
    assert "time.no_timezone" not in codes(stay)


def test_the_city_is_read_out_of_an_address_when_not_given_separately():
    from wayfare.validate import resolve

    stay = LodgingRecord(
        property_name="Some Hotel",
        location=Place(name="Some Hotel", address="Rue Neuve 1, Brussels"),
        check_in=LocalTime(local=datetime(2026, 3, 4, 15, 0)),
        check_out=LocalTime(local=datetime(2026, 3, 8, 11, 0)),
        extraction_confidence=0.8,
        provenance=provenance(),
    )
    resolve.run(Itinerary(records=[stay]))
    assert stay.location.timezone == "Europe/Brussels"


def test_an_unknown_place_still_warns_rather_than_inventing_a_zone():
    from wayfare.validate import resolve

    stay = LodgingRecord(
        property_name="Nowhere",
        location=Place(name="Nowhere", city="Qqqqqqx"),
        check_in=LocalTime(local=datetime(2026, 3, 4, 15, 0)),
        check_out=LocalTime(local=datetime(2026, 3, 8, 11, 0)),
        extraction_confidence=0.8,
        provenance=provenance(),
    )
    resolve.run(Itinerary(records=[stay]))
    assert stay.location.timezone is None
    assert "time.no_timezone" in codes(stay)


def test_a_one_way_trip_does_not_warn_about_checkout():
    """There is no return leg to be after, so the check has nothing to say."""
    itinerary = Itinerary(
        records=[
            flight("2026-03-04T09:35", "2026-03-04T12:25"),
            lodging("2026-03-04T15:00", "2026-03-08T11:00"),
        ]
    )
    coherence.run(itinerary)
    assert "lodging.checkout_after_departure" not in codes(itinerary.lodgings()[0])


def test_a_return_leg_still_catches_a_late_checkout():
    outbound = flight("2026-03-04T09:35", "2026-03-04T12:25")
    ret = flight(
        "2026-03-08T18:00",
        "2026-03-09T06:00",
        origin="JFK",
        destination="LHR",
        otz="America/New_York",
        dtz="Europe/London",
    )
    stay = lodging("2026-03-04T15:00", "2026-03-20T11:00")
    coherence.run(Itinerary(records=[outbound, ret, stay]))
    assert "lodging.checkout_after_departure" in codes(stay)
