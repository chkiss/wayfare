"""A leg that was never extracted has no fields to fail any other check.

The text here is from a real SATA Azores receipt that listed two flights and
produced one record, promoted to a live calendar at 90% confidence.
"""

from datetime import datetime

import pytest

from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    LodgingRecord,
    Place,
    Provenance,
)
from wayfare.validate import completeness

RECEIPT = """
From                        To                      Flight      Departure   Arrival
NEW YORK JOHN F KENNEDY     PONTA DELGADA JOAO      S4246       20:55       06:45
INTL                        PAULO II                            20Sep2026   21Sep2026
Terminal / Terminal: 1
Classe: BSC, O
Bagagem (4): 1PC

PONTA DELGADA JOAO PAULO II LISBON AIRPORT          S4120       08:25       11:40
                            Terminal / Terminal: 1              23Sep2026   23Sep2026
Classe: BSC, L
Telephone / Telefone: +351 296209720
"""


def flight(carrier="S4", number="120", **overrides):
    base = dict(
        carrier=carrier,
        number=number,
        origin=Place(iata="PDL", city="Ponta Delgada", timezone="Atlantic/Azores"),
        destination=Place(iata="LIS", city="Lisbon", timezone="Europe/Lisbon"),
        departure=LocalTime(local=datetime(2026, 9, 23, 8, 25), timezone="Atlantic/Azores"),
        arrival=LocalTime(local=datetime(2026, 9, 23, 11, 40), timezone="Europe/Lisbon"),
        provenance=Provenance(extractor="llm"),
        extraction_confidence=0.9,
    )
    base.update(overrides)
    return FlightRecord(**base)


def itinerary(records, text=RECEIPT, name="ticket.pdf"):
    trip = Itinerary()
    trip.records = list(records)
    trip.source_text = {name: text}
    return trip


# --- the failure this exists for ----------------------------------------


def test_a_leg_the_document_lists_but_nobody_extracted_is_caught():
    trip = completeness.run(itinerary([flight()]))
    assert any(i.code == "itinerary.leg_possibly_missing" for i in trip.issues)
    assert "S4246" in trip.issues[0].message


def test_the_warning_lands_on_the_records_so_nothing_is_promoted():
    """An itinerary-level issue holds nothing back; the point is to hold."""
    trip = completeness.run(itinerary([flight()]))
    (record,) = trip.records
    assert any(i.code == "itinerary.leg_possibly_missing" for i in record.issues)
    assert record.warnings


def test_both_legs_extracted_passes_cleanly():
    trip = completeness.run(itinerary([flight(number="120"), flight(number="246")]))
    assert not trip.issues
    assert not trip.records[0].warnings


# --- what must not trip it ----------------------------------------------


def test_a_phone_number_is_not_a_flight():
    trip = completeness.run(itinerary([flight(number="120"), flight(number="246")]))
    assert not trip.issues


def test_a_terminal_number_is_not_a_flight():
    text = "BA 117 to JFK\nTerminal / Terminal: 5\nClasse: BSC, O"
    trip = completeness.run(itinerary([flight(carrier="BA", number="117")], text=text))
    assert not trip.issues


def test_another_airlines_codes_are_not_our_missing_legs():
    """A page full of partner codes should not hold a correct submission."""
    text = "BA 117 JFK\nEarn Avios on AA 100, IB 342, QF 8\nSee ba.com"
    trip = completeness.run(itinerary([flight(carrier="BA", number="117")], text=text))
    assert not trip.issues


def test_a_time_of_day_is_not_a_service():
    """"10:26 AM 2:29 PM" read as service AM2 and held every Amtrak ticket."""
    text = "85 Sep 8, 2026 10:26 AM 2:29 PM"
    assert completeness.services_in(text) == []


# --- counting first ------------------------------------------------------


def test_the_document_is_counted_before_anyone_reads_it():
    """The scan is deterministic, so the reader can be given a checklist."""
    assert completeness.services_in(RECEIPT) == ["S4120", "S4246"]


def test_the_checklist_reaches_the_model():
    from wayfare.extractors import llm

    seen = {}

    def fake_call(text, cfg):
        seen["prompt"] = text
        return {"records": []}, "model:free"

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(llm, "_call_model", fake_call)
    monkeypatch.setattr(llm, "get_config", lambda: _KeyedConfig())
    try:
        llm.extract(RECEIPT, "ticket.pdf", None, expect=["S4120", "S4246"])
    finally:
        monkeypatch.undo()

    assert "checklist" in seen["prompt"]
    assert "S4246" in seen["prompt"]


def test_a_document_with_no_services_gets_no_checklist():
    from wayfare.extractors import llm

    seen = {}

    def fake_call(text, cfg):
        seen["prompt"] = text
        return {"records": []}, "model:free"

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(llm, "_call_model", fake_call)
    monkeypatch.setattr(llm, "get_config", lambda: _KeyedConfig())
    try:
        llm.extract("Hotel Example, 4-8 March", "hotel.eml", None, expect=[])
    finally:
        monkeypatch.undo()

    assert "checklist" not in seen["prompt"]


def test_every_leg_missing_is_caught_even_with_nothing_to_anchor_on():
    """No transport record at all leaves no carrier to filter by, and that is
    the most serious version of this failure, not a reason to stay quiet."""
    stay = LodgingRecord(
        property_name="Hotel Example",
        location=Place(name="Hotel Example", city="Lisbon"),
        check_in=LocalTime(local=datetime(2026, 9, 21, 15, 0), timezone="Europe/Lisbon"),
        check_out=LocalTime(local=datetime(2026, 9, 23, 11, 0), timezone="Europe/Lisbon"),
        provenance=Provenance(extractor="llm"),
    )
    trip = completeness.run(itinerary([stay]))
    assert any(i.code == "itinerary.leg_possibly_missing" for i in trip.issues)


def test_a_hotel_booking_is_not_checked_for_flight_numbers():
    stay = LodgingRecord(
        property_name="Hotel Example",
        location=Place(name="Hotel Example", city="Lisbon"),
        check_in=LocalTime(local=datetime(2026, 9, 21, 15, 0), timezone="Europe/Lisbon"),
        check_out=LocalTime(local=datetime(2026, 9, 23, 11, 0), timezone="Europe/Lisbon"),
        provenance=Provenance(extractor="llm"),
    )
    trip = completeness.run(itinerary([stay], text="Room 402, ref AB 1234, tel +351 21 000"))
    assert not trip.issues


def test_a_leading_zero_is_not_a_different_flight():
    """"S4 0120" and "S4120" are the same service."""
    trip = completeness.run(itinerary([flight(number="120")], text="Flight S4 0120 to Lisbon"))
    assert not trip.issues


def test_a_missing_rail_leg_is_caught_by_route_count():
    """A train number is bare, so only the routes can notice this."""
    from wayfare.schema import TrainRecord

    text = "BOS » NYP  10:26\nNYP » WAS  15:05\nTRAIN NORTHEAST REGIONAL"
    leg = TrainRecord(
        operator="Amtrak",
        number="85",
        origin=Place(name="South Station", city="Boston"),
        destination=Place(name="Penn Station", city="New York"),
        departure=LocalTime(local=datetime(2026, 9, 8, 10, 26), timezone="America/New_York"),
        provenance=Provenance(extractor="llm"),
    )
    trip = completeness.run(itinerary([leg], text=text))
    assert any(i.code == "itinerary.leg_possibly_missing" for i in trip.issues)


def test_a_complete_rail_ticket_is_not_flagged():
    """One route, one record. The real Amtrak ticket, which must stay clean."""
    from wayfare.schema import TrainRecord

    text = "BBY » NYP One-Way\nTRAIN NORTHEAST REGIONAL\n85 Sep 8, 2026 10:26 AM 2:29 PM"
    leg = TrainRecord(
        operator="Amtrak",
        number="85",
        origin=Place(name="Back Bay Station", city="Boston"),
        destination=Place(name="Penn Station", city="New York"),
        departure=LocalTime(local=datetime(2026, 9, 8, 10, 26), timezone="America/New_York"),
        provenance=Provenance(extractor="llm"),
    )
    assert not completeness.run(itinerary([leg], text=text)).issues


def test_no_source_text_means_no_opinion():
    trip = Itinerary()
    trip.records = [flight()]
    assert not completeness.run(trip).issues


# --- end to end ----------------------------------------------------------


def test_the_missing_leg_stops_promotion(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path))
    import wayfare.config as config

    from wayfare import store

    config._config = None

    from wayfare.validate import run_all

    trip = run_all(itinerary([flight()]))
    submission = store.commit(trip, "ticket.pdf", dry_run=True)
    (outcome,) = submission.to_dict()["records"]
    assert outcome["status"] == "pending"


def test_the_second_pass_asks_only_for_what_is_missing():
    from wayfare import pipeline
    from wayfare.ingest import ingest_text

    asked = {}

    def fake_extract(text, source_file, confidence, only=None):
        asked["only"] = only
        if not only:
            return [flight(number="120")]
        return [flight(number="246")]

    trip = Itinerary()
    ingested = ingest_text(RECEIPT, "ticket.pdf")

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(pipeline.llm_extractor, "extract", fake_extract)
    try:
        recovered = pipeline._second_pass_for_missing_legs(
            RECEIPT, ingested, [flight(number="120")], trip, []
        )
    finally:
        monkeypatch.undo()

    assert asked["only"] == ["S4246"]
    assert len(recovered) == 1
    assert any(i.code == "llm.second_pass" for i in trip.issues)


def test_no_second_pass_when_nothing_is_missing():
    from wayfare import pipeline
    from wayfare.ingest import ingest_text

    called = []

    def fake_extract(*args, **kwargs):
        called.append(kwargs.get("only"))
        return []

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(pipeline.llm_extractor, "extract", fake_extract)
    try:
        recovered = pipeline._second_pass_for_missing_legs(
            RECEIPT,
            ingest_text(RECEIPT, "ticket.pdf"),
            [flight(number="120"), flight(number="246")],
            Itinerary(),
            [],
        )
    finally:
        monkeypatch.undo()

    assert recovered == [] and called == []


def test_a_travel_document_that_yielded_nothing_is_read_again():
    """A Delta receipt produced no records at all; six later calls all worked."""
    from wayfare import pipeline
    from wayfare.ingest import ingest_text

    asked = {}

    def fake_extract(text, source_file, confidence, insist=False, **kwargs):
        asked["insist"] = insist
        return [flight()] if insist else []

    trip = Itinerary()
    text = "Flight DELTA 273 departs LISBON, PT 09:55AM arrives NYC-KENNEDY 12:58PM"

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(pipeline.llm_extractor, "extract", fake_extract)
    try:
        recovered = pipeline._second_pass_for_nothing_at_all(
            text, ingest_text(text, "pasted text"), trip
        )
    finally:
        monkeypatch.undo()

    assert asked["insist"] is True
    assert len(recovered) == 1
    assert any(i.code == "llm.second_pass" for i in trip.issues)


def test_a_document_about_nothing_is_not_read_again():
    """An empty screenshot is not worth a second model call."""
    from wayfare import pipeline
    from wayfare.ingest import ingest_text

    called = []

    def fake_extract(*args, **kwargs):
        called.append(True)
        return []

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(pipeline.llm_extractor, "extract", fake_extract)
    try:
        text = "Shopping list: milk, bread, 4 apples"
        pipeline._second_pass_for_nothing_at_all(
            text, ingest_text(text, "note.png"), Itinerary()
        )
    finally:
        monkeypatch.undo()

    assert called == []


def test_a_failed_second_pass_leaves_the_warning_standing():
    """The check is the safety net; the retry is the convenience."""
    from wayfare import pipeline
    from wayfare.ingest import ingest_text

    def explode(*args, **kwargs):
        raise RuntimeError("provider down")

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(pipeline.llm_extractor, "extract", explode)
    try:
        assert (
            pipeline._second_pass_for_missing_legs(
                RECEIPT, ingest_text(RECEIPT, "ticket.pdf"), [flight()], Itinerary(), []
            )
            == []
        )
    finally:
        monkeypatch.undo()


def test_the_follow_up_prompt_is_not_quotable_as_evidence():
    """Otherwise the second pass would verify itself against my own words."""
    from wayfare.extractors import llm

    seen = {}

    def fake_call(text, cfg):
        seen["prompt"] = text
        return {"records": []}, "model:free"

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(llm, "_call_model", fake_call)
    monkeypatch.setattr(llm, "get_config", lambda: _KeyedConfig())
    try:
        llm.extract("original text", "t.pdf", None, only=["S4246"])
    finally:
        monkeypatch.undo()

    assert "S4246" in seen["prompt"]
    assert "follow-up" in seen["prompt"]


class _KeyedConfig:
    llm_api_key = "key"
    llm_model = "model:free"


def test_the_model_that_read_it_is_named_on_the_event():
    record = flight(
        provenance=Provenance(extractor="llm", model="google/gemma-4-31b-it:free")
    )
    from wayfare.render import event_description

    assert "llm: google/gemma-4-31b-it:free" in event_description(record)


def test_a_merged_record_names_the_model_alongside_the_barcode():
    provenance = Provenance(extractor="barcode+llm", model="x/y:free")
    assert provenance.describe() == "barcode+llm: x/y:free"


def test_a_record_no_model_touched_is_unchanged():
    assert Provenance(extractor="barcode").describe() == "barcode"
