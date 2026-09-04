"""Reading the calendar attachment instead of guessing at it.

Every case here is a real layout from KItinerary's corpus. The file states the
journey exactly — an operator's own system wrote it — and wayfare was handing
it to a language model as raw text.
"""

from datetime import datetime

import pytest

from wayfare.extractors import icsevent


def calendar(*lines: str) -> str:
    return "\r\n".join(["BEGIN:VCALENDAR", "BEGIN:VEVENT", *lines, "END:VEVENT", "END:VCALENDAR"])


HAFAS = calendar(
    "SUMMARY:Frankfurt(Main)Hbf -> Paris Nord",
    "DTSTART;TZID=Europe/Berlin:20220726T102600",
    "DTEND;TZID=Europe/Berlin:20220726T160500",
    "DESCRIPTION:Reise: Frankfurt(Main)Hbf nach Paris Nord\\n"
    "ab 10:26 Frankfurt(Main)Hbf - Gleis 19 (ICE   16)\\n"
    "an 11:33 Köln Hbf - Gleis 6\\n\\n"
    "ab 12:42 Köln Hbf - Gleis 8 D-G (THA 9448)\\n"
    "an 16:05 Paris Nord \\n",
)


def test_a_connection_becomes_two_records():
    """The event says Frankfurt to Paris; the description says two trains."""
    first, second = icsevent.extract(HAFAS, "db.ics")

    assert (first.operator, first.number) == ("ICE", "16")
    assert (second.operator, second.number) == ("THA", "9448")
    assert first.origin.name == "Frankfurt(Main)Hbf"
    assert first.destination.name == "Köln Hbf"
    assert second.destination.name == "Paris Nord"


def test_the_times_come_from_the_leg_not_the_event():
    first, second = icsevent.extract(HAFAS, "db.ics")
    assert first.departure.local == datetime(2022, 7, 26, 10, 26)
    assert first.arrival.local == datetime(2022, 7, 26, 11, 33)
    assert second.departure.local == datetime(2022, 7, 26, 12, 42)
    assert first.departure.timezone == "Europe/Berlin"


def test_the_platform_is_not_part_of_the_station_name():
    (first, _) = icsevent.extract(HAFAS, "db.ics")
    assert "Gleis" not in first.origin.name


def test_another_language_is_read_without_being_taught_it():
    """Deutsche Bahn sends the traveller's own language: "de/a" in Spanish,
    "fra/til" in Danish. The shape is identical, so the first line's word is
    taken as this document's departure marker rather than guessed at."""
    spanish = calendar(
        "SUMMARY:Hamburg Hbf -> Dortmund Hbf",
        "DTSTART;TZID=Europe/Berlin:20220726T124600",
        "DESCRIPTION:Reise\\n"
        "de 12:46 Hamburg Hbf - Vía 14 (IC  2311)\\n"
        "a 15:00 Münster(Westf)Hbf - Vía 9\\n\\n"
        "de 15:10 Münster(Westf)Hbf - Vía 3 (RB 90027)\\n"
        "a 16:00 Dortmund Hbf - Vía 1\\n",
    )
    first, second = icsevent.extract(spanish, "db-es.ics")
    assert (first.operator, first.number) == ("IC", "2311")
    assert (second.operator, second.number) == ("RB", "90027")
    assert first.destination.name == "Münster(Westf)Hbf"


# --- airline calendars --------------------------------------------------


AIRLINE = calendar(
    "SUMMARY:Your flight from LHR to JFK",
    "DTSTART:20261201T131300Z",
    "DTEND:20261202T141400Z",
    "DESCRIPTION:Booking reference: 123XYZ \\n Operated by: Lufthansa \\n"
    " Flight number: LH 123 \\n",
)


def test_a_flight_is_read_with_its_number_and_reference():
    (record,) = icsevent.extract(AIRLINE, "lh.ics")
    assert (record.carrier, record.number) == ("LH", "123")
    assert record.confirmation == "123XYZ"
    assert record.origin.iata == "LHR"
    assert record.destination.iata == "JFK"


def test_a_utc_stamp_becomes_the_time_on_the_departure_board():
    """13:13Z at Heathrow is 13:13 local in winter; the point is that the zone
    is resolved rather than the number being taken at face value."""
    (record,) = icsevent.extract(AIRLINE, "lh.ics")
    assert record.departure.timezone == "Europe/London"
    assert record.arrival.timezone == "America/New_York"
    # Arrival is stated in UTC and read in New York's zone, five hours behind.
    assert record.arrival.local == datetime(2026, 12, 2, 9, 14)


# --- not everything with a code is a flight -----------------------------


def test_a_rail_station_code_is_not_taken_for_an_airport():
    """National Rail writes "Glasgow Central (GLC)", and GLC is Geladi Airport
    in Ethiopia. Looking the code up is not enough; the document has to say it
    is a flight first."""
    rail = calendar(
        "SUMMARY:Journey Details: Glasgow Central (GLC) to London Kings Cross (KGX)",
        "DTSTART:20230220T124000",
        "DTEND:20230220T173200",
        "DESCRIPTION:Train Company: Avanti West Coast",
    )
    (record,) = icsevent.extract(rail, "nr.ics")

    assert record.kind.value == "train"
    assert record.origin.iata is None
    assert record.origin.name == "Glasgow Central"
    assert record.origin.detail == "GLC"


def test_an_ordinary_appointment_is_not_a_journey():
    meeting = calendar(
        "SUMMARY:Standup with the team",
        "DTSTART;TZID=Europe/London:20260220T090000",
        "DESCRIPTION:Weekly",
    )
    assert icsevent.extract(meeting, "cal.ics") == []


def test_an_all_day_event_is_not_a_departure():
    holiday = calendar("SUMMARY:Trip to Paris", "DTSTART;VALUE=DATE:20260220")
    assert icsevent.extract(holiday, "cal.ics") == []


def test_a_file_that_is_not_a_calendar_is_left_alone():
    assert not icsevent.looks_like_calendar("Dear passenger, your flight...")
    assert icsevent.looks_like_calendar(HAFAS)


def test_a_platform_with_letters_in_it_is_still_stripped():
    """"Köln Hbf - Gleis 8 D-G" kept its platform, because that one does not
    end in a digit the way "Gleis 19" does."""
    lettered = calendar(
        "SUMMARY:Köln Hbf -> Paris Nord",
        "DTSTART;TZID=Europe/Berlin:20220726T124200",
        "DESCRIPTION:Reise\\n"
        "ab 12:42 Köln Hbf - Gleis 8 D-G (THA 9448)\\n"
        "an 16:05 Paris Nord \\n",
    )
    (record,) = icsevent.extract(lettered, "db.ics")
    assert record.origin.name == "Köln Hbf"


def test_a_station_whose_name_contains_a_dash_keeps_it():
    """The platform is recognised by carrying a number, not by the dash."""
    dashed = calendar(
        "SUMMARY:Baden-Baden -> Basel SBB",
        "DTSTART;TZID=Europe/Berlin:20220726T124200",
        "DESCRIPTION:Reise\\nab 12:42 Baden-Baden\\nan 14:05 Basel SBB\\n",
    )
    (record,) = icsevent.extract(dashed, "db.ics")
    assert record.origin.name == "Baden-Baden"
