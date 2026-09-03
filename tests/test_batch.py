"""Several documents submitted together are checked as one trip."""

from datetime import datetime

from wayfare.batch import combine
from wayfare.schema import (
    FlightRecord,
    Itinerary,
    LocalTime,
    LodgingRecord,
    Place,
    Provenance,
)


def flight(number, day, hour, arrive_hour, origin="LHR", destination="JFK", extractor="llm"):
    return FlightRecord(
        carrier="BA",
        number=number,
        origin=Place(iata=origin, city="London", timezone="Europe/London"),
        destination=Place(iata=destination, city="New York", timezone="America/New_York"),
        departure=LocalTime(local=datetime(2026, 3, day, hour, 0), timezone="Europe/London"),
        arrival=LocalTime(
            local=datetime(2026, 3, day, arrive_hour, 0), timezone="America/New_York"
        ),
        provenance=Provenance(extractor=extractor),
        extraction_confidence=0.85,
    )


def stay(check_in_day, check_out_day):
    return LodgingRecord(
        property_name="Hotel Example",
        location=Place(name="Hotel Example", city="New York", timezone="America/New_York"),
        check_in=LocalTime(
            local=datetime(2026, 3, check_in_day, 15, 0), timezone="America/New_York"
        ),
        check_out=LocalTime(
            local=datetime(2026, 3, check_out_day, 11, 0), timezone="America/New_York"
        ),
        provenance=Provenance(extractor="llm"),
        extraction_confidence=0.85,
    )


def wrap(*records):
    itinerary = Itinerary()
    itinerary.records = list(records)
    return itinerary


def test_a_round_trip_arriving_as_two_files_stays_two_legs():
    """The outbound and the return share a date-shaped identity but not a number."""
    combined = combine([wrap(flight("117", 4, 9, 12)), wrap(flight("118", 8, 18, 6))])
    assert len(combined.records) == 2
    assert [r.number for r in combined.records] == ["117", "118"]


def test_the_same_leg_in_two_documents_is_folded_together():
    """A confirmation email and its boarding pass are one flight, not two."""
    from_email = flight("117", 4, 9, 12)
    from_barcode = flight("117", 4, 9, 12, extractor="barcode")
    from_barcode.seat = "14A"

    combined = combine([wrap(from_email), wrap(from_barcode)])
    (leg,) = combined.records
    assert leg.seat == "14A"
    assert "barcode" in leg.provenance.extractor


def test_the_hotel_is_checked_against_flights_from_a_different_file():
    """The whole point of a batch: neither file is wrong on its own.

    The hotel starts three days before the flight lands. Submitted one at a
    time, both documents look perfectly consistent.
    """
    combined = combine(
        [wrap(flight("117", 4, 9, 12)), wrap(flight("118", 8, 18, 6)), wrap(stay(1, 8))]
    )
    codes = {issue.code for record in combined.records for issue in record.issues}
    assert "lodging.checkin_before_arrival" in codes


def test_records_come_back_in_trip_order_not_upload_order():
    combined = combine([wrap(flight("118", 8, 18, 6)), wrap(stay(4, 8)), wrap(flight("117", 4, 9, 12))])
    assert [r.kind.value for r in combined.records] == ["flight", "lodging", "flight"]


def test_a_single_document_is_not_double_checked():
    """Coherence already ran per file; running it again must not duplicate findings."""
    already = wrap(flight("117", 4, 9, 12), stay(1, 8))
    from wayfare.validate import coherence

    coherence.run(already, [])
    combined = combine([already])

    issues = [i.code for r in combined.records for i in r.issues]
    assert issues.count("lodging.checkin_before_arrival") == 1


# --- the command line ----------------------------------------------------


def test_the_cli_reads_several_files_as_one_trip(tmp_path):
    from types import SimpleNamespace

    from wayfare import cli

    for name in ("outbound.txt", "return.txt"):
        (tmp_path / name).write_text("nothing readable", encoding="utf-8")

    _, source = cli._read_input(
        SimpleNamespace(
            paths=[str(tmp_path / "outbound.txt"), str(tmp_path / "return.txt")], text=None
        )
    )
    assert source == "2 documents (outbound.txt, return.txt)"


def test_the_cli_still_names_a_lone_file_after_itself(tmp_path):
    from types import SimpleNamespace

    from wayfare import cli

    (tmp_path / "hotel.txt").write_text("nothing readable", encoding="utf-8")
    _, source = cli._read_input(SimpleNamespace(paths=[str(tmp_path / "hotel.txt")], text=None))
    assert source == "hotel.txt"
