"""The city is concluded, not read, and must not be judged as if it were read.

A boarding pass prints "LHR". It never prints "London". Holding the
city to the evidence rule discarded the one value the timezone lookup depends
on, and then reported the model for inventing it.
"""

from wayfare.extractors.llm import _build_record


SOURCE = "BA 117 LHR JFK Heathrow 10:00 30Sep2026 ref BBD03F"


def entry(**overrides):
    base = {
        "kind": "flight",
        "carrier": "BA",
        "number": "117",
        "origin_iata": "LHR",
        "destination_iata": "JFK",
        "origin_name": "London Heathrow Airport",
        "departure_local": "2026-09-30T10:00",
        "evidence": {
            "carrier": "BA",
            "number": "117",
            "origin_iata": "LHR",
            "destination_iata": "JFK",
            "origin_name": "Heathrow",
            "departure_local": "10:00 30Sep2026",
        },
    }
    base.update(overrides)
    return base


def build(**overrides):
    return _build_record(entry(**overrides), SOURCE, "ticket.pdf", None, "a:free")


def test_an_unquotable_city_is_not_reported_as_an_invention():
    record = build(origin_city="London", destination_city="New York")
    messages = [i.message for i in record.issues if i.code == "llm.unsupported_fields"]
    assert not messages


def test_an_unquotable_city_does_not_cost_confidence():
    """It used to halve it, which is what held a correctly read ticket."""
    assert build(origin_city="London").extraction_confidence == 0.85


def test_a_still_unquotable_field_is_discarded_as_before():
    """The rule is relaxed for cities only; everything else is unchanged."""
    record = build(seat="14C")
    assert record.seat is None
    assert "llm.unsupported_fields" in [i.code for i in record.issues]


def test_the_airport_database_overrules_the_model():
    """Which city LHR is in is a lookup, not a judgement."""
    record = build(origin_city="Reykjavik")
    assert record.origin.city == "London"


def test_the_city_is_filled_in_when_the_model_left_it_out():
    """Leaving it blank is the honest answer on a page that never prints it."""
    record = build()
    assert record.origin.city == "London"
    assert record.destination.city == "New York"


def test_a_concluded_city_is_still_named_as_a_conclusion():
    record = build(origin_city="London")
    (issue,) = [i for i in record.issues if i.code == "place.expanded_from_code"]
    assert "London" in issue.message
