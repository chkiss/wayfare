"""A flight number the document does not print is a misread, not a reading.

Measured on a SATA receipt printing "S4246": one reading returned carrier S4
and number 4246, having taken the carrier's digit twice. The result passes
every check — it is a plausible number, on the right route, at the right time
— and was promoted at 0.95. It sends you looking for a flight that does not
exist, and it splits one leg into two records.

The designators were scanned off the page deterministically before anybody
read it, so this needs no model.
"""

from datetime import datetime

from wayfare.pipeline import _correct_numbers_against_page
from wayfare.schema import FlightRecord, LocalTime, Place, Provenance


def flight(**overrides):
    base = dict(
        carrier="S4",
        number="4246",
        origin=Place(iata="JFK", city="New York"),
        destination=Place(iata="PDL", city="Ponta Delgada"),
        departure=LocalTime(local=datetime(2026, 9, 27, 20, 55)),
        provenance=Provenance(extractor="llm", model="a:free"),
    )
    base.update(overrides)
    return FlightRecord(**base)


def test_a_doubled_carrier_digit_is_corrected():
    record = flight(number="4246")
    _correct_numbers_against_page([record], ["S4246", "S4120"])
    assert record.number == "246"


def test_the_correction_is_reported():
    record = flight(number="4246")
    _correct_numbers_against_page([record], ["S4246"])
    (issue,) = [i for i in record.issues if i.code == "leg.number_corrected_from_page"]
    assert "S4246" in issue.message


def test_a_number_the_page_prints_is_left_alone():
    record = flight(number="246")
    _correct_numbers_against_page([record], ["S4246"])
    assert record.number == "246"
    assert not record.issues


def test_a_leg_the_scan_missed_is_not_overwritten():
    """This repairs a misread; it does not force every record onto the scan."""
    record = flight(number="871")
    _correct_numbers_against_page([record], ["S4246"])
    assert record.number == "871"


def test_an_ambiguous_correction_is_refused():
    """Two printed numbers both inside the misread settle nothing."""
    record = flight(number="41246")
    _correct_numbers_against_page([record], ["S4246", "S41"])
    assert record.number == "41246"


def test_another_carrier_is_not_consulted():
    record = flight(carrier="DL", number="4246")
    _correct_numbers_against_page([record], ["S4246"])
    assert record.number == "4246"


def test_nothing_happens_without_a_scan():
    record = flight(number="4246")
    _correct_numbers_against_page([record], [])
    assert record.number == "4246"
