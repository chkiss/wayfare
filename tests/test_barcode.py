from datetime import date

from wayfare.extractors import barcode

# A synthetic IATA BCBP payload. Fabricated, not a real booking: PNR ABC123,
# Montreal to Frankfurt on Air Canada 834, day 326 of the year, seat 4A.
SAMPLE = "M1DESMARAIS/LUC       E" "ABC123 " "YUL" "FRA" "AC " "0834 " "326" "J" "004A" "00025" "1" "00"


def test_sample_is_well_formed():
    assert len(SAMPLE) == 23 + 37


def test_parses_all_mandatory_fields():
    (leg,) = barcode.parse(SAMPLE, reference=date(2026, 11, 1))
    assert leg.passenger == "Luc Desmarais"
    assert leg.pnr == "ABC123"
    assert leg.origin == "YUL"
    assert leg.destination == "FRA"
    assert leg.carrier == "AC"
    assert leg.flight_number == "834"
    assert leg.seat == "4A"
    assert leg.flight_date == date(2026, 11, 22)


def test_julian_date_picks_the_nearest_year():
    """Day 326 read in January belongs to the year just ended, not this one."""
    (leg,) = barcode.parse(SAMPLE, reference=date(2027, 1, 10))
    assert leg.flight_date == date(2026, 11, 22)


def test_non_boarding_pass_payload_is_ignored():
    assert barcode.parse("https://example.com/booking/12345") == []
    assert barcode.parse("") == []


def test_finds_payload_printed_as_text():
    assert barcode.find_in_text(f"Boarding pass\n{SAMPLE}\nGate 12") == [SAMPLE]


def test_record_carries_date_but_flags_missing_time():
    records = barcode.to_records(barcode.parse(SAMPLE, reference=date(2026, 11, 1)), "pass.png")
    (record,) = records
    assert record.departure.local.date() == date(2026, 11, 22)
    assert record.provenance.extractor == "barcode"
    assert any(i.code == "barcode.time_not_encoded" for i in record.issues)
