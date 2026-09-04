"""Reading a value through OCR noise, without loosening the numbers.

Every case here is taken from one real Amtrak eTicket screenshot, whose OCR
read "Moynitan Train Hall at Penn Sta" and "RES# BBDO3F" where the barcode
said "BBD03F".
"""

from wayfare.extractors.llm import (
    FUZZY_THRESHOLD,
    _build_record,
    _fuzzy_in,
    _normalise,
    _verify_evidence,
)

OCR = _normalise(
    """
    RES# BBDO3F-O7AUG26
    BBY » NYP One-Way
    Boston, MA New York, NY SEPTEMBER 8, 2026
    Back Bay Station Moynitan Train Hall at Penn Sta
    TRAIN NORTHEAST REGIONAL, DEPARTS. ARRIVES
    85 Sep 8, 2026 10:26 AM 2:29 PM
    AMTRAK GUEST REWARDS No member number provided. Join at Amtrak.com
    """
)


def entry(**overrides):
    base = {
        "kind": "train",
        "operator": "Amtrak",
        "number": "85",
        "origin_name": "Back Bay Station",
        "destination_name": "Moynihan Train Hall at Penn Station",
        "departure_local": "2026-09-08T10:26",
        "evidence": {
            "operator": "Amtrak",
            "number": "85",
            "origin_name": "Back Bay Station",
            "destination_name": "Moynihan Train Hall at Penn Station",
            "departure_local": "10:26 AM",
        },
    }
    base.update(overrides)
    return base


# --- the correction that was being thrown away --------------------------


def test_a_station_name_corrected_from_garbled_ocr_is_accepted():
    """"Moynitan ... Penn Sta" is plainly the same place, and better spelled."""
    supported, unsupported, corrected = _verify_evidence(entry(), OCR)

    assert "destination_name" in supported
    assert "destination_name" in corrected
    assert not unsupported


def test_the_record_keeps_the_corrected_name():
    record = _build_record(entry(), OCR, "ticket.png", None)
    assert record.destination.name == "Moynihan Train Hall at Penn Station"


def test_the_correction_is_declared_rather_than_hidden():
    record = _build_record(entry(), OCR, "ticket.png", None)
    assert any(i.code == "llm.corrected_from_source" for i in record.issues)


def test_an_exact_quote_is_not_reported_as_corrected():
    _, _, corrected = _verify_evidence(entry(), OCR)
    assert "origin_name" not in corrected


# --- what must stay exact ------------------------------------------------


def test_a_booking_reference_is_never_fuzzy_matched():
    """OCR read BBDO3F, the barcode says BBD03F. Those are different strings."""
    bad = entry(confirmation="BBD03F", evidence={**entry()["evidence"], "confirmation": "BBD03F"})
    _, unsupported, _ = _verify_evidence(bad, OCR)
    assert "confirmation" in unsupported


def test_a_time_is_never_fuzzy_matched():
    """10:26 and 10:20 differ by one character and by six minutes."""
    bad = entry(evidence={**entry()["evidence"], "departure_local": "10:20 AM"})
    _, unsupported, _ = _verify_evidence(bad, OCR)
    assert "departure_local" in unsupported


# --- a quote assembled from two columns ---------------------------------

TWO_COLUMN = _normalise(
    """
    From                     To                    Flight   Departure   Arrival
    NEW YORK JOHN F KENNEDY  PONTA DELGADA         S4246    20:55       06:45
    INTL                     JOAO PAULO II                  20Sep2026   21Sep2026
    """
)


def leg(**overrides):
    base = {
        "kind": "flight",
        "carrier": "S4",
        "number": "246",
        "departure_local": "2026-09-20T20:55",
        "evidence": {"carrier": "S4246", "number": "S4246", "departure_local": "20:55 20Sep2026"},
    }
    base.update(overrides)
    return base


def test_a_time_and_date_from_different_columns_still_count():
    """The page prints both, in different places. The quote joins them."""
    supported, unsupported, _ = _verify_evidence(leg(), TWO_COLUMN)
    assert "departure_local" in supported
    assert not unsupported


def test_the_leg_survives_being_built():
    """With no departure there is no record at all, which is how one vanished."""
    record = _build_record(leg(), TWO_COLUMN, "ticket.pdf", None)
    assert record is not None
    assert record.departure.local.hour == 20


def test_an_invented_hour_still_fails_when_assembled():
    bad = leg(evidence={**leg()["evidence"], "departure_local": "18:40 20Sep2026"})
    _, unsupported, _ = _verify_evidence(bad, TWO_COLUMN)
    assert "departure_local" in unsupported


def test_an_invented_date_still_fails_when_assembled():
    bad = leg(evidence={**leg()["evidence"], "departure_local": "20:55 29Sep2026"})
    _, unsupported, _ = _verify_evidence(bad, TWO_COLUMN)
    assert "departure_local" in unsupported


def test_a_single_missing_token_is_not_assembly():
    """One token that is not on the page is absent, not scattered."""
    bad = leg(confirmation="ZZZZZZ", evidence={**leg()["evidence"], "confirmation": "ZZZZZZ"})
    _, unsupported, _ = _verify_evidence(bad, TWO_COLUMN)
    assert "confirmation" in unsupported


def test_a_service_number_is_never_fuzzy_matched():
    bad = entry(number="86", evidence={**entry()["evidence"], "number": "86"})
    _, unsupported, _ = _verify_evidence(bad, OCR)
    assert "number" in unsupported


# --- the threshold itself ------------------------------------------------


def test_a_different_station_does_not_pass_as_a_correction():
    assert not _fuzzy_in(_normalise("Grand Central Terminal"), OCR)


def test_a_short_quote_is_never_fuzzy_matched():
    """Three characters can resemble anything; only a real phrase can vouch."""
    assert not _fuzzy_in("nyp", OCR)


def test_an_abbreviation_printed_on_the_ticket_vouches_for_the_full_name():
    """The page says "Penn Sta". The station is Penn Station."""
    assert _fuzzy_in(_normalise("Penn Station"), OCR)


def test_a_prefix_must_be_most_of_the_name():
    """"Penn" appears on the page and vouches for nothing much."""
    assert not _fuzzy_in(_normalise("Penn Station Pennsylvania Avenue Concourse"), OCR)


def test_the_expanded_name_reaches_the_title():
    record = _build_record(entry(destination_name="Penn Station"), OCR, "ticket.png", None)
    from wayfare.render import event_summary

    assert record.destination.name == "Penn Station"
    assert event_summary(record).endswith("→ Penn Station")


def test_the_threshold_is_a_deliberate_value():
    assert 0.75 <= FUZZY_THRESHOLD <= 0.9


def test_the_search_finds_a_match_anywhere_in_a_long_document():
    padded = "unrelated text. " * 400 + OCR
    assert _fuzzy_in(_normalise("Moynihan Train Hall at Penn Station"), padded)
