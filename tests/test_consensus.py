"""Two models reading the same document, and what to do when they differ.

Every case here is a failure actually measured on a free model: a value
dropped, a whole leg dropped, a whole reading dropped. None of them is a wrong
value, which is why verification against the document cannot catch any of them.
"""

from datetime import datetime

import pytest

from wayfare.extractors import consensus
from wayfare.schema import (
    FlightRecord,
    LocalTime,
    LodgingRecord,
    Place,
    Provenance,
)


def flight(model="a:free", confidence=0.85, **overrides):
    base = dict(
        carrier="DL",
        number="273",
        origin=Place(iata="LIS", city="Lisbon"),
        destination=Place(iata="JFK", city="New York"),
        departure=LocalTime(local=datetime(2026, 9, 27, 9, 55), timezone="Europe/Lisbon"),
        arrival=LocalTime(local=datetime(2026, 9, 27, 12, 58), timezone="America/New_York"),
        provenance=Provenance(extractor="llm", model=model),
        extraction_confidence=confidence,
    )
    base.update(overrides)
    return FlightRecord(**base)


MODELS = ["a:free", "b:free"]


# --- recovering what one model dropped ----------------------------------


def test_a_flight_number_one_model_dropped_is_recovered():
    """Measured: one run in four returned "LIS → New York (DL )"."""
    lost = flight(number=None, confidence=0.85)
    intact = flight(number="273", confidence=0.85)

    (record,) = consensus.reconcile([[lost], [intact]], MODELS)
    assert record.number == "273"


def test_a_whole_leg_one_model_dropped_is_kept():
    """Measured: a two-flight receipt returned one record."""
    outbound = flight(number="273")
    ret = flight(
        number="274",
        departure=LocalTime(local=datetime(2026, 10, 4, 18, 0), timezone="America/New_York"),
    )

    records = consensus.reconcile([[outbound], [outbound, ret]], MODELS)
    assert sorted(r.number for r in records) == ["273", "274"]


def test_a_whole_reading_that_came_back_empty_costs_nothing():
    """Measured: a Delta receipt read correctly six times and returned nothing once."""
    (record,) = consensus.reconcile([[], [flight()]], MODELS)
    assert record.number == "273"


def test_a_leg_only_one_model_saw_is_marked_as_such():
    ret = flight(
        number="274",
        departure=LocalTime(local=datetime(2026, 10, 4, 18, 0), timezone="America/New_York"),
    )
    records = consensus.reconcile([[flight()], [flight(), ret]], MODELS)
    lonely = [r for r in records if r.number == "274"][0]
    assert any(i.code == "consensus.one_model_only" for i in lonely.issues)


def test_a_place_detail_one_model_dropped_is_recovered():
    without = flight(destination=Place(iata="JFK", city="New York"))
    with_hall = flight(
        destination=Place(iata="JFK", city="New York", detail="Terminal 4"),
    )
    (record,) = consensus.reconcile([[without], [with_hall]], MODELS)
    assert record.destination.detail == "Terminal 4"


# --- agreement -----------------------------------------------------------


def test_agreement_is_recorded_and_earns_confidence():
    (record,) = consensus.reconcile([[flight()], [flight()]], MODELS)
    assert any(i.code == "consensus.models_agree" for i in record.issues)
    # The only positive evidence in the pipeline, so it counts towards promotion.
    assert "consensus.models_agree" in record.CONFIRMATIONS


def test_both_models_are_named_on_the_record():
    (record,) = consensus.reconcile([[flight()], [flight(model="b:free")]], MODELS)
    assert record.provenance.model == "a:free + b:free"


def test_case_and_spacing_do_not_count_as_disagreement():
    (record,) = consensus.reconcile(
        [[flight(traveller="Charles Kissick")], [flight(traveller="CHARLES KISSICK ")]],
        MODELS,
    )
    assert not any(i.code == "consensus.models_disagree" for i in record.issues)


# --- disagreement --------------------------------------------------------


def test_a_disputed_time_is_reported_rather_than_chosen_silently():
    early = flight(
        departure=LocalTime(local=datetime(2026, 9, 27, 9, 55), timezone="Europe/Lisbon"),
        confidence=0.85,
    )
    late = flight(
        departure=LocalTime(local=datetime(2026, 9, 27, 10, 55), timezone="Europe/Lisbon"),
        confidence=0.45,
    )

    (record,) = consensus.reconcile([[early], [late]], MODELS)
    disputes = [i for i in record.issues if i.code == "consensus.models_disagree"]
    assert disputes
    assert "09:55" in disputes[0].message and "10:55" in disputes[0].message


def test_a_dispute_holds_the_record_for_review():
    a = flight(seat="14A", confidence=0.85)
    b = flight(seat="15B", confidence=0.85)
    (record,) = consensus.reconcile([[a], [b]], MODELS)
    assert record.warnings


def test_the_more_fully_quoted_reading_wins_a_dispute():
    """Not a coin toss: the reading that quoted more of the document leads."""
    thorough = flight(seat="14A", confidence=0.85)
    partial = flight(seat="15B", confidence=0.45)
    (record,) = consensus.reconcile([[thorough], [partial]], MODELS)
    assert record.seat == "14A"


# --- identity ------------------------------------------------------------


def test_two_readings_of_one_flight_are_one_record():
    assert len(consensus.reconcile([[flight()], [flight()]], MODELS)) == 1


def test_a_leading_zero_does_not_split_a_flight_in_two():
    assert len(consensus.reconcile([[flight(number="273")], [flight(number="0273")]], MODELS)) == 1


def test_two_flights_on_one_day_stay_separate():
    morning = flight(number="273")
    evening = flight(
        number="9999",
        departure=LocalTime(local=datetime(2026, 9, 27, 19, 0), timezone="Europe/Lisbon"),
    )
    assert len(consensus.reconcile([[morning, evening], [morning, evening]], MODELS)) == 2


def test_a_flight_with_no_number_is_matched_on_its_route():
    """One model dropped the number, so identity has to fall back to the route."""
    numberless = flight(number=None)
    numbered = flight(number="273")
    assert len(consensus.reconcile([[numberless], [numbered]], MODELS)) == 1


def test_a_hotel_is_not_confused_with_a_flight_on_the_same_day():
    stay = LodgingRecord(
        property_name="Hotel Example",
        location=Place(name="Hotel Example", city="Lisbon"),
        check_in=LocalTime(local=datetime(2026, 9, 27, 15, 0), timezone="Europe/Lisbon"),
        check_out=LocalTime(local=datetime(2026, 9, 29, 11, 0), timezone="Europe/Lisbon"),
        provenance=Provenance(extractor="llm"),
    )
    assert len(consensus.reconcile([[flight(), stay], [flight(), stay]], MODELS)) == 2


# --- running them --------------------------------------------------------


def test_both_models_are_asked():
    asked = []

    def fake(model, text, source_file, confidence, **kwargs):
        asked.append(model)
        return [flight(model=model)]

    readings, used = consensus.read("text", "f.pdf", None, MODELS, fake)
    assert sorted(asked) == ["a:free", "b:free"]
    assert len(readings) == 2 and used == MODELS


def test_one_model_failing_does_not_lose_the_other():
    def fake(model, text, source_file, confidence, **kwargs):
        if model == "a:free":
            raise RuntimeError("rate limited")
        return [flight(model=model)]

    readings, used = consensus.read("text", "f.pdf", None, MODELS, fake)
    assert len(readings) == 1 and used == ["b:free"]


def test_a_slow_second_opinion_does_not_hold_up_the_first():
    """Parallel still means waiting for the slowest, so the wait is bounded."""
    import time as _time

    def fake(model, text, source_file, confidence, **kwargs):
        if model == "b:free":
            _time.sleep(5)
        return [flight(model=model)]

    started = _time.monotonic()
    readings, used = consensus.read(
        "text", "f.pdf", None, MODELS, fake, grace_seconds=0.3
    )
    elapsed = _time.monotonic() - started

    assert used == ["a:free"]
    assert len(readings) == 1
    assert elapsed < 3


def test_a_second_opinion_inside_the_window_is_used():
    import time as _time

    def fake(model, text, source_file, confidence, **kwargs):
        if model == "b:free":
            _time.sleep(0.2)
        return [flight(model=model)]

    readings, used = consensus.read("text", "f.pdf", None, MODELS, fake, grace_seconds=5)
    assert sorted(used) == ["a:free", "b:free"]
    assert len(readings) == 2


def test_a_spare_stands_in_for_a_model_that_refuses():
    """Measured: a two-model quorum returned one reading in nine seconds,
    the other having been rate limited instantly."""

    def fake(model, text, source_file, confidence, **kwargs):
        if model == "a:free":
            raise RuntimeError("429 rate limited")
        return [flight(model=model)]

    readings, used = consensus.read(
        "text", "f.pdf", None, ["a:free", "b:free", "c:free"], fake, want=2
    )
    assert sorted(used) == ["b:free", "c:free"]
    assert len(readings) == 2


def test_spares_are_abandoned_once_enough_have_answered():
    asked = []

    def fake(model, text, source_file, confidence, **kwargs):
        asked.append(model)
        return [flight(model=model)]

    _, used = consensus.read(
        "text", "f.pdf", None, ["a:free", "b:free", "c:free", "d:free"], fake, want=2
    )
    assert len(used) == 2


def test_a_single_reading_is_returned_unchanged():
    """Nothing to compare against, so nothing is added or claimed."""
    (record,) = consensus.reconcile([[flight()]], ["a:free"])
    assert not any(i.code.startswith("consensus.") for i in record.issues)


def test_no_readings_at_all_is_not_a_crash():
    assert consensus.reconcile([], []) == []
