"""Surface transport, station timezones, and the reviewable event text."""

from datetime import datetime

from wayfare.icswrite import record_to_ics
from wayfare.render import DEFAULT_CONVENTIONS
from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
    TrainRecord,
)
from wayfare.validate import geo, resolve
from wayfare.validate.resolve import _city_guesses


def train(mode="train", origin="Back Bay Station", destination="New York Penn Station", hours=4):
    return TrainRecord(
        mode=mode,
        operator="Amtrak",
        number="85",
        origin=Place(name=origin),
        destination=Place(name=destination),
        departure=LocalTime(local=datetime(2026, 3, 4, 9, 15)),
        arrival=LocalTime(local=datetime(2026, 3, 4, 9 + hours, 15)),
        provenance=Provenance(extractor="llm"),
        extraction_confidence=0.85,
    )


def wrap(record):
    itinerary = Itinerary()
    itinerary.records = [record]
    return itinerary


# --- a station is not a city --------------------------------------------


def test_a_station_name_yields_the_city_inside_it():
    assert _city_guesses(Place(name="Boston South Station")) == ["Boston South", "Boston"]


def test_station_words_are_stripped_across_languages():
    assert "Frankfurt" in _city_guesses(Place(name="Frankfurt (Main) Hbf"))
    assert "Bruxelles" in _city_guesses(Place(name="Bruxelles-Midi"))


def test_a_leading_connector_is_dropped():
    """Without this, "Gare de Lyon" reduces to "de Lyon" and matches nothing."""
    assert _city_guesses(Place(name="Gare de Lyon"))[0] == "Lyon"


def test_a_city_named_by_the_model_resolves_the_timezone():
    """The model is asked for the city precisely so this can happen."""
    record = train()
    record.origin.city = "Boston"
    record.destination.city = "New York"
    resolve.run(wrap(record))

    assert record.origin.timezone == "America/New_York"
    assert record.departure.timezone == "America/New_York"


def test_a_station_only_ticket_still_gets_a_timezone():
    """No city given: the timezone has to come out of the station name."""
    record = train(origin="Boston South Station", destination="Providence Station")
    resolve.run(wrap(record))
    assert record.departure.timezone == "America/New_York"


def test_a_station_guess_never_becomes_the_city():
    """Gare de Lyon is in Paris. Good enough for a timezone, not for a label."""
    record = train(origin="Gare de Lyon", destination="Bruxelles-Midi")
    resolve.run(wrap(record))
    assert record.origin.city != "Lyon"


# --- modes ---------------------------------------------------------------


def test_a_coach_doing_rail_speeds_is_flagged():
    """300 km/h is fine for a train and impossible for a bus."""
    fast = train(mode="bus", origin="Boston", destination="New York", hours=1)
    fast.origin.city, fast.destination.city = "Boston", "New York"
    resolve.run(wrap(fast))
    geo.run(wrap(fast))

    assert any(i.code == "leg.faster_than_possible" for i in fast.issues)


def test_the_same_journey_by_train_is_not_flagged():
    ok = train(mode="train", origin="Boston", destination="New York", hours=4)
    ok.origin.city, ok.destination.city = "Boston", "New York"
    resolve.run(wrap(ok))
    geo.run(wrap(ok))

    assert not any(i.level.value == "error" for i in ok.issues)


def test_the_message_names_the_mode_not_just_rail():
    ferry = train(mode="ferry", origin="Boston", destination="New York", hours=1)
    ferry.origin.city, ferry.destination.city = "Boston", "New York"
    resolve.run(wrap(ferry))
    geo.run(wrap(ferry))

    assert any("ferry" in i.message for i in ferry.issues)


def test_an_endpoint_that_was_never_read_is_named_as_missing():
    """This is the case that produced "Back Bay Station → ?" on the calendar."""
    record = train(destination="")
    record.destination = Place()
    resolve.run(wrap(record))
    assert any(i.code == "leg.destination_not_read" for i in record.issues)


def test_the_model_may_call_a_coach_a_bus_or_a_coach():
    from wayfare.extractors.llm import GROUND_MODES

    assert GROUND_MODES["coach"] == "bus"
    assert GROUND_MODES["rail"] == "train"


# --- the reviewable event text ------------------------------------------


def flight():
    return FlightRecord(
        carrier="BA",
        number="117",
        origin=Place(iata="LHR", city="London", name="London Heathrow Airport"),
        destination=Place(iata="JFK", city="New York", name="John F Kennedy"),
        departure=LocalTime(local=datetime(2026, 3, 4, 9, 35), timezone="Europe/London"),
        arrival=LocalTime(local=datetime(2026, 3, 4, 12, 25), timezone="America/New_York"),
        seat="14A",
        provenance=Provenance(extractor="barcode"),
    )


def test_the_ics_carries_both_zones_not_one_converted_instant():
    text = record_to_ics(flight(), DEFAULT_CONVENTIONS)
    assert "DTSTART;TZID=Europe/London:20260304T093500" in text
    assert "DTEND;TZID=America/New_York:20260304T122500" in text
    assert "Z\r\n" not in text  # nothing collapsed to UTC


def test_the_ics_shows_the_title_location_and_reminders():
    text = record_to_ics(flight(), DEFAULT_CONVENTIONS)
    assert "SUMMARY:LHR → New York (BA 117)" in text
    assert "LOCATION:London Heathrow Airport" in text
    assert "TRIGGER:-PT180M" in text and "TRIGGER:-PT45M" in text


def test_commas_and_newlines_are_escaped():
    record = flight()
    record.destination.city = "New York, NY"
    text = record_to_ics(record, DEFAULT_CONVENTIONS)
    assert "New York\\, NY" in text
    assert "DESCRIPTION:" in text and "\\n" in text


def test_long_lines_are_folded_the_way_the_format_requires():
    record = flight()
    record.origin.name = "A" * 200
    text = record_to_ics(record, DEFAULT_CONVENTIONS)
    for line in text.split("\r\n"):
        assert len(line) <= 75


def test_a_record_with_no_usable_time_yields_nothing():
    record = flight()
    record.departure = None
    assert record_to_ics(record, DEFAULT_CONVENTIONS) == ""


def test_the_ics_reaches_the_review_screen(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path))
    import wayfare.config as config

    from wayfare import store

    config._config = None
    submission = store.commit(wrap(flight()), "boarding-pass.png", dry_run=True)
    (record,) = submission.to_dict()["records"]
    assert record["ics"].startswith("BEGIN:VCALENDAR")
