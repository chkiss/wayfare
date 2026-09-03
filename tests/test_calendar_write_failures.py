"""A record Google will not accept must not take the trip down with it."""

from datetime import datetime

import pytest

from wayfare import store
from wayfare.render import DEFAULT_CONVENTIONS, fallback_timezone, to_google_events
from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
)


def flight(number="117", zone="Europe/London"):
    return FlightRecord(
        carrier="BA",
        number=number,
        origin=Place(iata="LHR", city="London", timezone=zone),
        destination=Place(iata="JFK", city="New York", timezone=zone),
        departure=LocalTime(local=datetime(2026, 3, 4, 9, 35), timezone=zone),
        arrival=LocalTime(local=datetime(2026, 3, 4, 12, 25), timezone=zone),
        provenance=Provenance(extractor="barcode"),
        extraction_confidence=0.95,
    )


# --- the missing timezone ------------------------------------------------


def test_an_unresolved_zone_still_produces_a_writable_event():
    """Google rejects a naive dateTime outright: "Missing time zone definition"."""
    (body,) = to_google_events(flight(zone=None), DEFAULT_CONVENTIONS)
    assert body["start"]["timeZone"]
    assert body["end"]["timeZone"]


def test_a_known_zone_is_never_overridden_by_the_fallback():
    (body,) = to_google_events(flight(zone="Europe/London"), DEFAULT_CONVENTIONS)
    assert body["start"]["timeZone"] == "Europe/London"


def test_the_fallback_zone_is_configurable():
    conventions = dict(DEFAULT_CONVENTIONS, default_timezone="Asia/Tokyo")
    assert fallback_timezone(conventions) == "Asia/Tokyo"
    (body,) = to_google_events(flight(zone=None), conventions)
    assert body["start"]["timeZone"] == "Asia/Tokyo"


def test_the_calendars_own_zone_is_preferred_over_the_hosts(tmp_path, monkeypatch):
    """The server's timezone is a fact about the hosting, not about the user."""
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path))
    import wayfare.config as config

    config._config = None

    class _InTokyo(_RejectsOne):
        def calendar_timezone(self):
            return "Asia/Tokyo"

    trip = Itinerary()
    trip.records = [flight("117", zone=None)]
    client = _InTokyo("nothing")
    store.commit(trip, "trip", client=client)

    assert client.created[0]["start"]["timeZone"] == "Asia/Tokyo"


def test_the_host_zone_is_an_iana_name():
    from wayfare.timeutil import host_timezone

    name = host_timezone()
    assert name and "\n" not in name
    from zoneinfo import ZoneInfo

    ZoneInfo(name)  # raises if it is not a real zone


# --- one failed write ----------------------------------------------------


class _RejectsOne:
    """A calendar that refuses one particular event and accepts the rest."""

    def __init__(self, bad_number):
        self.bad_number = bad_number
        self.created = []

    def pending_calendar_id(self):
        return "pending"

    def target_calendar_id(self):
        return "target"

    def create(self, body, calendar_id, colour_id=None):
        if self.bad_number in body["summary"]:
            raise RuntimeError(
                'returned "Missing time zone definition for start time.". Details: [...]'
            )
        self.created.append(body)
        return {"id": f"event-{len(self.created)}"}

    def move(self, event_id, source, destination):
        return {"id": event_id}


@pytest.fixture
def itinerary():
    trip = Itinerary()
    trip.records = [flight("117"), flight("118"), flight("119")]
    return trip


def test_the_legs_either_side_of_a_refused_event_are_still_written(itinerary, tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path))
    import wayfare.config as config

    config._config = None

    client = _RejectsOne("118")
    submission = store.commit(itinerary, "trip", client=client)

    assert len(client.created) == 2
    assert [o.status for o in submission.outcomes] == ["promoted", "rejected", "promoted"]


def test_the_refusal_says_what_google_actually_objected_to(itinerary, tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path))
    import wayfare.config as config

    config._config = None

    submission = store.commit(itinerary, "trip", client=_RejectsOne("118"))
    refused = submission.outcomes[1]

    assert "Missing time zone definition" in refused.reason
    # Not the whole request URL and JSON body.
    assert "https://" not in refused.reason
    assert refused.issues[-1]["code"] == "calendar.write_failed"
