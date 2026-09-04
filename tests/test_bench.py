"""Scoring the tool against documents whose answers are known.

The point of this harness is to answer "did that change help", so its own
arithmetic has to be trustworthy — above all it must never make a document
that produced nothing look like a document that produced nothing wrong.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from wayfare import bench
from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
)


FLIGHT_ANSWER = [
    {
        "@type": "FlightReservation",
        "reservationNumber": "XXX007",
        "reservationFor": {
            "@type": "Flight",
            "airline": {"@type": "Airline", "iataCode": "LH"},
            "flightNumber": "123",
            "departureAirport": {"@type": "Airport", "iataCode": "FRA"},
            "arrivalAirport": {"@type": "Airport", "iataCode": "EWR"},
            "departureTime": {"@value": "2026-12-01T14:13:00+01:00"},
            "arrivalTime": {"@value": "2026-12-02T09:14:00-05:00"},
        },
    }
]


def flight(**overrides):
    base = dict(
        carrier="LH",
        number="123",
        origin=Place(iata="FRA", city="Frankfurt"),
        destination=Place(iata="EWR", city="Newark"),
        departure=LocalTime(local=datetime(2026, 12, 1, 14, 13), timezone="Europe/Berlin"),
        arrival=LocalTime(local=datetime(2026, 12, 2, 9, 14), timezone="America/New_York"),
        confirmation="XXX007",
        provenance=Provenance(extractor="llm", model="a:free"),
    )
    base.update(overrides)
    return FlightRecord(**base)


@pytest.fixture
def corpus(tmp_path):
    (tmp_path / "lufthansa").mkdir()
    document = tmp_path / "lufthansa" / "5_FRA-EWR.txt"
    document.write_text("LH 123 FRA EWR", encoding="utf-8")
    (tmp_path / "lufthansa" / "5_FRA-EWR.txt.json").write_text(
        json.dumps(FLIGHT_ANSWER), encoding="utf-8"
    )
    return tmp_path


# --- reading the corpus -------------------------------------------------


def test_a_document_is_paired_with_its_answer(corpus):
    (case,) = bench.load_corpus(corpus)
    assert case.category == "flight"
    (want,) = case.expected
    assert (want.carrier, want.number, want.origin, want.destination) == (
        "LH", "123", "FRA", "EWR",
    )
    assert want.start == datetime(2026, 12, 1, 14, 13)


def test_the_offset_is_dropped_rather_than_converted():
    """Comparing through UTC would turn a timezone bug into a passing test."""
    assert bench._stamp({"@value": "2026-12-02T09:14:00-05:00"}) == datetime(2026, 12, 2, 9, 14)


def test_formats_the_tool_cannot_read_are_not_scored(corpus):
    """A raw UIC barcode is not a wayfare failure; it is not a wayfare input."""
    (corpus / "rail").mkdir()
    (corpus / "rail" / "ticket.bin").write_bytes(b"\x00\x01")
    (corpus / "rail" / "ticket.bin.json").write_text(json.dumps(FLIGHT_ANSWER), encoding="utf-8")
    assert [c.path.suffix for c in bench.load_corpus(corpus)] == [".txt"]


# --- scoring ------------------------------------------------------------


def test_a_perfect_reading_scores_every_field(corpus):
    (case,) = bench.load_corpus(corpus)
    result = bench.compare(case, Itinerary(records=[flight()]))
    assert result.count_ok
    assert all(got == total for got, total in result.fields.values())


def test_finding_nothing_fails_every_stated_field(corpus):
    """The failure this whole pipeline exists to catch must not score zero
    out of zero, which would read as 100%."""
    (case,) = bench.load_corpus(corpus)
    result = bench.compare(case, Itinerary(records=[]))

    assert result.found == 0
    assert not result.count_ok
    assert result.fields["number"] == (0, 1)
    assert result.fields["start"] == (0, 1)
    assert all(got == 0 for got, _ in result.fields.values())


def test_a_wrong_number_is_caught(corpus):
    (case,) = bench.load_corpus(corpus)
    result = bench.compare(case, Itinerary(records=[flight(number="1123")]))
    assert result.fields["number"] == (0, 1)
    assert result.fields["origin"] == (1, 1)


def test_a_leading_zero_is_not_a_difference(corpus):
    (case,) = bench.load_corpus(corpus)
    result = bench.compare(case, Itinerary(records=[flight(number="0123")]))
    assert result.fields["number"] == (1, 1)


def test_an_hour_read_wrong_is_caught(corpus):
    (case,) = bench.load_corpus(corpus)
    late = flight(departure=LocalTime(local=datetime(2026, 12, 1, 4, 13), timezone="Europe/Berlin"))
    result = bench.compare(case, Itinerary(records=[late]))
    assert result.fields["start"] == (0, 1)


def test_legs_are_matched_by_fit_not_by_order(corpus):
    """A reading that returns the return leg first is not wrong about it."""
    answer = FLIGHT_ANSWER + [
        {
            "@type": "FlightReservation",
            "reservationFor": {
                "@type": "Flight",
                "flightNumber": "456",
                "departureAirport": {"@type": "Airport", "iataCode": "EWR"},
                "arrivalAirport": {"@type": "Airport", "iataCode": "FRA"},
                "departureTime": {"@value": "2026-12-09T18:00:00-05:00"},
            },
        }
    ]
    (corpus / "lufthansa" / "5_FRA-EWR.txt.json").write_text(json.dumps(answer), encoding="utf-8")
    (case,) = bench.load_corpus(corpus)

    ret = flight(
        number="456",
        origin=Place(iata="EWR"),
        destination=Place(iata="FRA"),
        departure=LocalTime(local=datetime(2026, 12, 9, 18, 0)),
        arrival=None,
    )
    result = bench.compare(case, Itinerary(records=[ret, flight()]))
    assert result.fields["number"] == (2, 2)


def test_a_station_abbreviated_differently_still_counts():
    """Operators abbreviate their own stations; the corpus is not a style guide."""
    assert bench._same_place("MONTPELLIER ST-RO", flight(
        destination=Place(name="Montpellier Saint-Roch")), "destination") is True
    assert bench._same_place("Toulouse Matabiau", flight(
        destination=Place(name="Paris Gare de Lyon")), "destination") is False


def test_a_field_the_corpus_does_not_state_is_not_scored(corpus):
    """Otherwise the tool is marked down for the corpus being incomplete."""
    (case,) = bench.load_corpus(corpus)
    case.expected[0].confirmation = None
    result = bench.compare(case, Itinerary(records=[flight(confirmation="ANYTHING")]))
    assert "confirmation" not in result.fields


def test_the_summary_adds_up(corpus):
    (case,) = bench.load_corpus(corpus)
    results = [
        bench.compare(case, Itinerary(records=[flight()])),
        bench.compare(case, Itinerary(records=[])),
    ]
    summary = bench.summarise(results)
    assert summary["documents"] == 2
    assert summary["categories"]["flight"]["right_count"] == 1
    assert summary["fields"]["number"] == (1, 2)


def test_the_model_is_off_unless_asked_for(corpus, monkeypatch):
    """A free tier allows ~50 requests a day; a hundred documents is more."""
    seen = {}

    def fake_process(path, name=None, existing_events=None):
        from wayfare.extractors import llm

        seen["available"] = llm.available()
        return Itinerary()

    import wayfare.pipeline as pipeline

    monkeypatch.setattr(pipeline, "process_file", fake_process)
    monkeypatch.setenv("WAYFARE_LLM_API_KEY", "sk-test")
    bench.run(corpus)
    assert seen["available"] is False


def test_the_switch_is_put_back_afterwards(corpus, monkeypatch):
    import wayfare.pipeline as pipeline

    monkeypatch.setattr(pipeline, "process_file", lambda *a, **k: Itinerary())
    monkeypatch.delenv("WAYFARE_DISABLE_LLM", raising=False)
    bench.run(corpus)
    import os

    assert "WAYFARE_DISABLE_LLM" not in os.environ
