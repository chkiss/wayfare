"""What the review list shows, and what it stops showing."""

from datetime import datetime

import pytest

from wayfare import store
from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
    TrainRecord,
)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path))
    import wayfare.config as config

    config._config = None
    yield


class _Calendar:
    def pending_calendar_id(self):
        return "pending"

    def target_calendar_id(self):
        return "target"

    def create(self, body, calendar_id, colour_id=None):
        return {"id": "event-" + body["summary"][:4]}

    def move(self, event_id, source, destination):
        return {"id": event_id}

    def delete(self, event_id, calendar_id):
        return None


def flight(number="117"):
    return FlightRecord(
        carrier="BA",
        number=number,
        origin=Place(iata="LHR", city="London"),
        destination=Place(iata="JFK", city="New York"),
        departure=LocalTime(local=datetime(2026, 3, 4, 9, 35), timezone="Europe/London"),
        arrival=LocalTime(local=datetime(2026, 3, 4, 12, 25), timezone="America/New_York"),
        provenance=Provenance(extractor="barcode"),
        extraction_confidence=0.95,
    )


def trip(*records):
    itinerary = Itinerary()
    itinerary.records = list(records)
    return itinerary


# --- discarding ----------------------------------------------------------


def test_a_discarded_record_leaves_the_list():
    submission = store.commit(trip(flight("117"), flight("118")), "trip", client=_Calendar())
    store.discard(submission.submission_id, 0, client=_Calendar())

    (listed,) = store.recent()
    assert [r["summary"] for r in listed["records"]] == ["LHR → New York (BA 118)"]


def test_a_submission_with_nothing_left_disappears_entirely():
    submission = store.commit(trip(flight("117")), "trip", client=_Calendar())
    store.discard(submission.submission_id, 0, client=_Calendar())
    assert store.recent() == []


def test_the_record_is_kept_on_disk():
    """Hidden from the list, not deleted: the audit trail is the point."""
    submission = store.commit(trip(flight("117")), "trip", client=_Calendar())
    store.discard(submission.submission_id, 0, client=_Calendar())

    assert store.load(submission.submission_id)["records"][0]["status"] == "discarded"
    assert len(store.recent(include_discarded=True)) == 1


def test_the_surviving_records_keep_their_stored_positions():
    """The index addresses promote and discard. Filtering must not renumber it."""
    submission = store.commit(
        trip(flight("117"), flight("118"), flight("119")), "trip", client=_Calendar()
    )
    store.discard(submission.submission_id, 0, client=_Calendar())

    (listed,) = store.recent()
    assert [r["index"] for r in listed["records"]] == [1, 2]

    # Acting on the index shown must reach the record shown.
    store.discard(submission.submission_id, listed["records"][0]["index"], client=_Calendar())
    stored = store.load(submission.submission_id)["records"]
    assert [r["status"] for r in stored] == ["discarded", "discarded", "promoted"]


def test_the_limit_counts_submissions_that_are_actually_shown():
    for index in range(4):
        submission = store.commit(trip(flight(str(100 + index))), f"trip-{index}", client=_Calendar())
        if index < 2:
            store.discard(submission.submission_id, 0, client=_Calendar())

    assert len(store.recent(limit=2)) == 2


# --- the label of last resort -------------------------------------------


def test_a_city_is_used_when_the_station_name_was_discarded():
    """The real case: the printed name was unquotable, the city was not."""
    leg = TrainRecord(
        operator="Amtrak",
        number="85",
        origin=Place(name="Back Bay Station", city="Boston"),
        destination=Place(city="New York"),
        departure=LocalTime(local=datetime(2026, 3, 4, 9, 15), timezone="America/New_York"),
        provenance=Provenance(extractor="llm"),
    )
    assert leg.destination.label() == "New York"

    from wayfare.render import event_summary

    assert "→ New York" in event_summary(leg)


def test_a_place_with_nothing_known_still_says_so():
    assert Place().label() == "?"


def test_a_name_still_beats_a_city():
    assert Place(name="Back Bay Station", city="Boston").label() == "Back Bay Station"
