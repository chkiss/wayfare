from datetime import datetime

from wayfare.render import (
    DEFAULT_CONVENTIONS,
    event_reminders,
    event_summary,
    to_google_events,
)
from wayfare.schema import (
    FlightRecord,
    LocalTime,
    LodgingRecord,
    Place,
    Provenance,
)


def flight(**overrides):
    base = dict(
        carrier="AF",
        number="3611",
        origin=Place(iata="LHR", city="London", name="London Heathrow Airport"),
        destination=Place(iata="CDG", city="Paris", name="Charles de Gaulle Airport"),
        departure=LocalTime(local=datetime(2026, 3, 4, 9, 35), timezone="Europe/London"),
        arrival=LocalTime(local=datetime(2026, 3, 4, 15, 5), timezone="Europe/Paris"),
        provenance=Provenance(extractor="llm"),
    )
    base.update(overrides)
    return FlightRecord(**base)


def conventions(**overrides):
    merged = dict(DEFAULT_CONVENTIONS)
    merged.update(overrides)
    return merged


# --- titles --------------------------------------------------------------


def test_city_tokens_render_the_learned_google_style_title():
    c = conventions(flight_title="Flight to {destination_city} ({carrier} {number})")
    assert event_summary(flight(), c) == "Flight to Paris (AF 3611)"


def test_city_falls_back_to_the_code_when_no_city_is_known():
    record = flight(destination=Place(iata="CDG"))
    c = conventions(flight_title="Flight to {destination_city}")
    assert event_summary(record, c) == "Flight to CDG"


def test_the_location_field_carries_the_full_airport_name():
    """Codes read fast in a title; the location field has to work in Maps."""
    (body,) = to_google_events(flight(), conventions())
    assert body["location"] == "London Heathrow Airport"


# --- reminders -----------------------------------------------------------


def test_flights_get_two_reminders_by_default():
    reminders = event_reminders(flight(), conventions())
    assert reminders["useDefault"] is False
    assert [o["minutes"] for o in reminders["overrides"]] == [180, 45]


def test_reminder_times_are_configurable():
    reminders = event_reminders(flight(), conventions(flight_reminders_minutes=[240, 90, 30]))
    assert [o["minutes"] for o in reminders["overrides"]] == [240, 90, 30]


def test_an_empty_list_restores_the_calendar_default():
    assert event_reminders(flight(), conventions(flight_reminders_minutes=[])) == {
        "useDefault": True
    }


def test_reminders_are_sorted_deduplicated_and_capped():
    """Google rejects more than five, and anything over four weeks out."""
    reminders = event_reminders(
        flight(),
        conventions(flight_reminders_minutes=[45, 180, 45, 999999, 10, 20, 30, 60]),
    )
    minutes = [o["minutes"] for o in reminders["overrides"]]
    assert minutes == sorted(minutes, reverse=True)
    assert 999999 not in minutes
    assert len(minutes) == 5
    assert minutes.count(45) == 1


def test_rubbish_reminder_values_do_not_break_the_write():
    reminders = event_reminders(flight(), conventions(flight_reminders_minutes=["soon", None]))
    assert reminders == {"useDefault": True}


def test_reminders_reach_the_event_body():
    (body,) = to_google_events(flight(), conventions())
    assert body["reminders"]["overrides"][0]["minutes"] == 180


def test_hotels_have_no_reminders_unless_asked_for():
    stay = LodgingRecord(
        property_name="Hotel Example",
        location=Place(name="Hotel Example", city="Paris"),
        check_in=LocalTime(local=datetime(2026, 3, 4, 15, 0), timezone="Europe/London"),
        check_out=LocalTime(local=datetime(2026, 3, 8, 11, 0), timezone="Europe/London"),
        provenance=Provenance(extractor="llm"),
    )
    assert event_reminders(stay, conventions()) == {"useDefault": True}


def test_both_endpoint_events_carry_the_reminders():
    stay = LodgingRecord(
        property_name="Hotel Example",
        location=Place(name="Hotel Example"),
        check_in=LocalTime(local=datetime(2026, 3, 4, 15, 0), timezone="Europe/London"),
        check_out=LocalTime(local=datetime(2026, 3, 8, 11, 0), timezone="Europe/London"),
        provenance=Provenance(extractor="llm"),
    )
    events = to_google_events(
        stay, conventions(lodging_style="endpoints", lodging_reminders_minutes=[120])
    )
    assert len(events) == 2
    assert all(e["reminders"]["overrides"][0]["minutes"] == 120 for e in events)
