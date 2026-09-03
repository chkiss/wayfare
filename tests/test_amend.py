"""Correcting a held record in place.

Uses a stand-in for the calendar client so the whole review loop is testable
without credentials or network.
"""

from datetime import datetime

import pytest

from wayfare import store
from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
)


class FakeCalendar:
    """Records what would have been written, and answers reads from that."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.moves: list[tuple[str, str, str]] = []
        self._next = 0

    def pending_calendar_id(self):
        return "pending-calendar"

    def target_calendar_id(self):
        return "real-calendar"

    def create(self, body, calendar_id, colour_id=None):
        self._next += 1
        event_id = f"evt{self._next}"
        self.events[event_id] = {**body, "id": event_id, "_calendar": calendar_id}
        return self.events[event_id]

    def get_event(self, event_id, calendar_id):
        return self.events[event_id]

    def patch(self, event_id, calendar_id, changes):
        self.events[event_id].update(changes)
        return self.events[event_id]

    def move(self, event_id, source, destination):
        self.moves.append((event_id, source, destination))
        self.events[event_id]["_calendar"] = destination
        return self.events[event_id]

    def delete(self, event_id, calendar_id):
        self.events.pop(event_id, None)

    def context_window(self, records):
        return []


def held_flight():
    """A barcode-derived flight: route and date certain, time unknown."""
    record = FlightRecord(
        carrier="AC",
        number="834",
        origin=Place(iata="YUL", city="Montréal", timezone="America/Toronto"),
        destination=Place(iata="FRA", city="Frankfurt", timezone="Europe/Berlin"),
        departure=LocalTime(local=datetime(2026, 11, 22, 0, 0), timezone="America/Toronto"),
        extraction_confidence=0.99,
        provenance=Provenance(extractor="barcode", source_file="pass.png"),
    )
    record.add_issue(
        __import__("wayfare.schema", fromlist=["IssueLevel"]).IssueLevel.WARN,
        "flight.no_departure_time",
        "No time could be read.",
        "pipeline",
    )
    return record


@pytest.fixture
def submission():
    calendar = FakeCalendar()
    result = store.commit(
        Itinerary(records=[held_flight()]), "pass.png", client=calendar, allow_promote=True
    )
    return result, calendar


def test_a_record_with_a_warning_is_held_not_promoted(submission):
    result, calendar = submission
    assert result.outcomes[0].status == "pending"
    assert calendar.moves == []


def test_amending_the_start_time_patches_the_event(submission):
    result, calendar = submission
    record = store.amend(
        result.submission_id, 0, start="2026-11-22T18:40", client=calendar
    )
    event = calendar.events["evt1"]
    assert event["start"]["dateTime"] == "2026-11-22T18:40:00"
    assert record["edited"] is True


def test_the_resolved_timezone_survives_an_edit(submission):
    """The zone came from the airport database; a retyped time must not lose it."""
    result, calendar = submission
    store.amend(result.submission_id, 0, start="2026-11-22T18:40", client=calendar)
    assert calendar.events["evt1"]["start"]["timeZone"] == "America/Toronto"


def test_the_warning_that_asked_for_the_edit_is_cleared(submission):
    result, calendar = submission
    record = store.amend(result.submission_id, 0, start="2026-11-22T18:40", client=calendar)
    assert not any(i["code"] == "flight.no_departure_time" for i in record["issues"])


def test_an_end_before_the_new_start_is_pushed_forward(submission):
    """Google rejects an event that ends before it begins."""
    result, calendar = submission
    store.amend(result.submission_id, 0, start="2026-11-22T18:40", client=calendar)
    event = calendar.events["evt1"]
    assert event["end"]["dateTime"] >= event["start"]["dateTime"]


def test_an_unreadable_time_is_refused_rather_than_guessed(submission):
    result, calendar = submission
    with pytest.raises(store.AmendError):
        store.amend(result.submission_id, 0, start="next tuesday", client=calendar)


def test_editing_the_title_works_on_its_own(submission):
    result, calendar = submission
    record = store.amend(result.submission_id, 0, summary="YUL → Frankfurt (AC 834)", client=calendar)
    assert record["summary"] == "YUL → Frankfurt (AC 834)"


def test_amend_then_promote_moves_it_to_the_real_calendar(submission):
    result, calendar = submission
    store.amend(result.submission_id, 0, start="2026-11-22T18:40", client=calendar)
    store.promote(result.submission_id, 0, client=calendar)
    assert calendar.moves and calendar.moves[0][1] == "pending-calendar"


def test_a_two_event_record_is_refused_with_a_reason(submission):
    result, calendar = submission
    data = store.load(result.submission_id)
    data["records"][0]["event_ids"] = ["evt1", "evt2"]
    store.get_config().records_dir.joinpath(f"{result.submission_id}.json").write_text(
        __import__("json").dumps(data)
    )
    with pytest.raises(store.AmendError, match="two events"):
        store.amend(result.submission_id, 0, start="2026-11-22T18:40", client=calendar)
