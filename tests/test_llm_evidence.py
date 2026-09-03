"""The evidence rule is the reason a weak free model can be trusted here."""

from wayfare.extractors import llm

SOURCE = """\
British Airways
BA 117  London Heathrow (LHR) -> New York JFK
Depart 09:35   Arrive 12:25
Booking reference: ABC123
"""


def entry(**overrides):
    base = {
        "kind": "flight",
        "carrier": "BA",
        "number": "117",
        "origin_iata": "LHR",
        "destination_iata": "JFK",
        "departure_local": "2026-03-04T09:35",
        "confirmation": "ABC123",
        "evidence": {
            "carrier": "BA",
            "number": "117",
            "origin_iata": "LHR",
            "destination_iata": "JFK",
            "departure_local": "09:35",
            "confirmation": "ABC123",
        },
    }
    base.update(overrides)
    return base


def test_quoted_fields_are_accepted():
    supported, unsupported = llm._verify_evidence(entry(), SOURCE)
    assert not unsupported
    assert "departure_local" in supported


def test_invented_field_without_a_quote_is_rejected():
    bad = entry(seat="14A")  # No evidence entry for "seat" at all.
    supported, unsupported = llm._verify_evidence(bad, SOURCE)
    assert "seat" in unsupported


def test_invented_field_with_a_fabricated_quote_is_rejected():
    bad = entry()
    bad["arrival_local"] = "2026-03-04T18:40"
    bad["evidence"]["arrival_local"] = "Arrive 18:40"  # Not in the source.
    supported, unsupported = llm._verify_evidence(bad, SOURCE)
    assert "arrival_local" in unsupported


def test_unsupported_values_never_reach_the_record():
    bad = entry(seat="14A")
    record = llm._build_record(bad, SOURCE, "test.png", ocr_confidence=0.9)
    assert record is not None
    assert record.seat is None
    assert any(i.code == "llm.unsupported_fields" for i in record.issues)


def test_quote_matching_survives_ocr_whitespace_noise():
    noisy = SOURCE.replace("Booking reference: ABC123", "Booking   reference:   ABC123")
    supported, unsupported = llm._verify_evidence(entry(), noisy)
    assert "confirmation" in supported


def test_reply_wrapped_in_a_code_fence_is_still_parsed():
    parsed = llm._parse_json('```json\n{"records": []}\n```')
    assert parsed == {"records": []}
