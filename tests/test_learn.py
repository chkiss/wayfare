from datetime import datetime

from wayfare.icsparse import VEvent, parse, unfold
from wayfare.learn import classify, collect, learn


def event(summary, start="2026-03-04T15:00", end="2026-03-08T11:00", location="", description=""):
    return VEvent(
        summary=summary,
        location=location,
        description=description,
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
    )


# --- ics parsing ---------------------------------------------------------


def test_folded_lines_are_rejoined():
    """RFC 5545 wraps long values onto continuation lines starting with a space."""
    assert unfold("SUMMARY:BA117 London\r\n  Heathrow") == ["SUMMARY:BA117 London Heathrow"]


def test_parses_an_event_with_a_timezone_and_escapes():
    events = parse(
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:BA117 LHR \\, JFK\r\n"
        "DESCRIPTION:Seat: 14A\\nConfirmation: ABC123\r\n"
        "DTSTART;TZID=Europe/London:20260304T093500\r\n"
        "DTEND;TZID=America/New_York:20260304T122500\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    (parsed,) = events
    assert parsed.summary == "BA117 LHR , JFK"
    assert "\n" in parsed.description
    assert parsed.start == datetime(2026, 3, 4, 9, 35)
    assert parsed.start_tz == "Europe/London"


def test_all_day_events_are_recognised():
    (parsed,) = parse(
        "BEGIN:VEVENT\r\nSUMMARY:Trip\r\nDTSTART;VALUE=DATE:20260304\r\n"
        "DTEND;VALUE=DATE:20260308\r\nEND:VEVENT\r\n"
    )
    assert parsed.all_day is True
    assert parsed.duration_days == 4


# --- classification ------------------------------------------------------


def test_flight_with_route_is_a_flight():
    assert classify(event("✈ BA117 LHR → JFK")) == "flight"


def test_rail_is_not_mistaken_for_a_flight():
    """'Eurostar 9145 BRU - LON' matches the route pattern too."""
    assert classify(event("Eurostar 9145 BRU - LON")) == "rail"


def test_hotel_without_the_word_hotel_is_still_a_stay():
    """Most hotels are not called 'hotel'. Shape has to carry the decision."""
    assert classify(event("Hyatt Regency", location="Hyatt Regency, Chicago")) == "lodging"


def test_a_stay_needs_check_in_hours_not_just_length():
    """A week-long conference is not a hotel booking."""
    conference = event(
        "PyCon", start="2026-03-04T09:00", end="2026-03-08T17:00", location="Convention Centre"
    )
    assert classify(conference) is None


def test_ordinary_appointments_are_ignored():
    assert classify(event("Dentist", start="2026-03-04T09:00", end="2026-03-04T10:00")) is None


# --- learning ------------------------------------------------------------


def sample_calendar():
    events = []
    for index in range(30):
        events.append(
            event(
                f"✈ BA{100 + index} LHR → JFK",
                start="2026-03-04T09:35",
                end="2026-03-04T17:25",
                location="London Heathrow",
                description=f"Confirmation: ABC{index}\nSeat: 14A",
            )
        )
    for index in range(12):
        events.append(event(f"🏨 Hotel Example {index}", location="Brussels"))
    return events


def test_conventions_follow_the_calendar():
    conventions, text = learn(sample_calendar())
    assert conventions["title_prefix"] == "✈ "
    assert "→" in conventions["flight_title"]
    assert conventions["lodging_style"] == "span"
    assert conventions["include_confirmation"] is True
    assert "🏨" in conventions["lodging_title"]
    assert "flights 30" in text


def test_a_thin_calendar_says_so_rather_than_guessing_confidently():
    _, text = learn([event("✈ BA117 LHR → JFK", location="LHR")])
    assert "fewer than 20 travel events" in text


def test_unrelated_events_do_not_become_conventions():
    findings = collect([event("Dentist", start="2026-03-04T09:00", end="2026-03-04T10:00")])
    assert findings.flights == [] and findings.lodging == []
