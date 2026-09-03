"""A clean record has to be able to reach the promotion threshold."""

from datetime import datetime

from wayfare.config import get_config
from wayfare.schema import (
    FlightRecord,
    IssueLevel,
    LocalTime,
    Place,
    Provenance,
)


def record(extraction=0.85, ocr=None):
    return FlightRecord(
        carrier="BA",
        number="117",
        origin=Place(iata="LHR", timezone="Europe/London"),
        destination=Place(iata="CDG", timezone="Europe/Paris"),
        departure=LocalTime(local=datetime(2026, 10, 2, 8, 15), timezone="Europe/London"),
        arrival=LocalTime(local=datetime(2026, 10, 2, 13, 55), timezone="Europe/Paris"),
        extraction_confidence=extraction,
        provenance=Provenance(extractor="llm", ocr_confidence=ocr),
    )


def test_a_fully_quoted_record_meets_the_promotion_threshold():
    """Otherwise nothing a model reads could ever be promoted."""
    assert record().confidence() >= get_config().promote_threshold


def test_a_confirmed_block_time_earns_extra_confidence():
    plain = record()
    confirmed = record()
    confirmed.add_issue(IssueLevel.INFO, "leg.block_time_ok", "consistent", "geo")
    assert confirmed.confidence() > plain.confidence()


def test_a_warning_drops_it_below_the_threshold():
    held = record()
    held.add_issue(IssueLevel.WARN, "leg.block_time_implausible", "odd", "geo")
    assert held.confidence() < get_config().promote_threshold


def test_bad_ocr_caps_a_confident_extraction():
    assert record(ocr=0.5).confidence() <= 0.5


def test_an_error_zeroes_it():
    broken = record()
    broken.add_issue(IssueLevel.ERROR, "leg.faster_than_possible", "impossible", "geo")
    assert broken.confidence() == 0.0
