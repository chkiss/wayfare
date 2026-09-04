"""The last resort: the person holding the ticket settles it.

Two readings differed, the model that read the document could not quote a line
that decided it, so the question goes to somebody who can simply look. A
warning nobody can act on is a worse answer than a question with two buttons.
"""

from datetime import datetime

import pytest

from wayfare import store
from wayfare.config import get_config
from wayfare.schema import (
    TrainRecord,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
)


class FakeCalendar:
    """Enough of the client to see what would have been written."""

    def __init__(self):
        self.patched = []

    def pending_calendar_id(self):
        return "pending"

    def target_calendar_id(self):
        return "real"

    def calendar_timezone(self):
        return "Europe/Lisbon"

    def create(self, body, calendar_id, colour_id=None):
        return {"id": f"ev{len(self.patched)}"}

    def move(self, event_id, source, target):
        return {"id": event_id}

    def patch(self, event_id, calendar_id, changes):
        self.patched.append((event_id, changes))
        return {"id": event_id, **changes}


def train(**overrides):
    """A rail ticket, because that is where the disputed name actually shows.

    A flight's title uses the airport code, so a disagreement about the
    airport's full name never reaches the calendar. A station has no code, and
    the name is the title.
    """
    base = dict(
        mode="train",
        operator="Amtrak",
        number="2151",
        origin=Place(city="Boston", name="Back Bay Station"),
        destination=Place(city="New York", name="Penn Station"),
        departure=LocalTime(local=datetime(2026, 9, 27, 9, 55), timezone="America/New_York"),
        arrival=LocalTime(local=datetime(2026, 9, 27, 12, 58), timezone="America/New_York"),
        provenance=Provenance(extractor="llm", model="a:free"),
        extraction_confidence=0.85,
    )
    base.update(overrides)
    return TrainRecord(**base)


DISPUTE = {
    "field": "destination.name",
    "values": ["Penn Station", "Moynihan Train Hall at Penn Station"],
    "chosen": None,
}


@pytest.fixture
def submitted(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path / "state"))
    import wayfare.config as config

    config._config = None
    record = train()
    record.disputes = [dict(DISPUTE)]
    itinerary = Itinerary(records=[record])
    calendar = FakeCalendar()
    submission = store.commit(itinerary, "ticket.pdf", client=calendar)
    return submission.submission_id, calendar


def test_the_choice_is_offered_with_both_readings(submitted):
    submission_id, _ = submitted
    (stored,) = store.load(submission_id)["records"]
    assert stored["disputes"][0]["values"] == DISPUTE["values"]


def test_choosing_re_renders_the_event(submitted):
    submission_id, calendar = submitted
    stored = store.resolve_dispute(
        submission_id,
        0,
        "destination.name",
        "Moynihan Train Hall at Penn Station",
        client=calendar,
    )

    assert stored["record"]["destination"]["name"] == "Moynihan Train Hall at Penn Station"
    # Re-rendered, not patched: the pasteable event agrees with the title.
    assert "Moynihan Train Hall at Penn Station" in stored["ics"]
    assert stored["disputes"][0]["chosen"] == "Moynihan Train Hall at Penn Station"


def test_the_calendar_is_corrected_too(submitted):
    submission_id, calendar = submitted
    store.resolve_dispute(
        submission_id, 0, "destination.name",
        "Moynihan Train Hall at Penn Station", client=calendar,
    )
    assert calendar.patched, "the event on the calendar still shows the other reading"
    _, changes = calendar.patched[0]
    assert "summary" in changes


def test_the_warning_goes_once_every_question_is_answered(submitted):
    submission_id, calendar = submitted
    data = store.load(submission_id)
    data["records"][0]["issues"] = [
        {"level": "warn", "code": "consensus.models_disagree", "message": "...", "source": "c"}
    ]
    (get_config().records_dir / f"{submission_id}.json").write_text(
        __import__("json").dumps(data), encoding="utf-8"
    )

    stored = store.resolve_dispute(
        submission_id, 0, "destination.name", "Penn Station", client=calendar
    )
    assert [i["code"] for i in stored["issues"]] == []


def test_a_value_neither_reading_produced_is_refused(submitted):
    """A tie-break between two readings, not a free text field."""
    submission_id, calendar = submitted
    with pytest.raises(store.AmendError):
        store.resolve_dispute(
            submission_id, 0, "destination.name", "Grand Central", client=calendar
        )


def test_a_field_nobody_disputed_is_refused(submitted):
    submission_id, calendar = submitted
    with pytest.raises(store.AmendError):
        store.resolve_dispute(submission_id, 0, "confirmation", "GBUQV6", client=calendar)
