"""What was read, what the barcode said, and not saying it all twice."""

from datetime import datetime

from wayfare import pipeline, store
from wayfare.ingest import ingest_text
from wayfare.schema import (
    FlightRecord,
    IssueLevel,
    Itinerary,
    LocalTime,
    Place,
    Provenance,
)


def flight(**overrides):
    base = dict(
        carrier="BA",
        number="117",
        origin=Place(iata="LHR", city="London"),
        destination=Place(iata="JFK", city="New York"),
        departure=LocalTime(local=datetime(2026, 3, 4, 9, 35), timezone="Europe/London"),
        arrival=LocalTime(local=datetime(2026, 3, 4, 12, 25), timezone="America/New_York"),
        provenance=Provenance(extractor="barcode"),
        extraction_confidence=0.95,
    )
    base.update(overrides)
    return FlightRecord(**base)


# --- the duplicated sentence --------------------------------------------


def test_the_reason_does_not_repeat_the_warning_below_it():
    record = flight()
    record.add_issue(IssueLevel.WARN, "test.one", "The destination looks wrong.", "test")

    status, reason = store.decide(record)
    assert status == "pending"
    assert "The destination looks wrong." not in reason
    assert "1 thing to check" in reason


def test_the_reason_counts_the_warnings():
    record = flight()
    record.add_issue(IssueLevel.WARN, "test.one", "First.", "test")
    record.add_issue(IssueLevel.WARN, "test.two", "Second.", "test")
    assert "2 things to check" in store.decide(record)[1]


# --- what was read -------------------------------------------------------


TICKET = "Amtrak 85 departs Boston BBY 09:15 4 March 2026, arrives NYP 13:40."


def test_the_text_the_reader_saw_is_kept(monkeypatch):
    monkeypatch.setattr(pipeline.llm_extractor, "extract", lambda *a, **k: [])
    itinerary = pipeline._process(ingest_text(TICKET, "ticket.png"), source_path=None)
    assert itinerary.source_text["ticket.png"] == TICKET


def test_it_is_kept_even_when_nothing_could_be_extracted(monkeypatch):
    """The case where seeing what was read is the entire diagnosis."""
    monkeypatch.setattr(pipeline.llm_extractor, "extract", lambda *a, **k: [])
    itinerary = pipeline._process(ingest_text("nothing here", "blank.png"), source_path=None)

    assert itinerary.records == []
    assert itinerary.source_text["blank.png"] == "nothing here"


def test_it_survives_being_combined_into_a_batch(monkeypatch):
    from wayfare.batch import combine

    monkeypatch.setattr(pipeline.llm_extractor, "extract", lambda *a, **k: [])
    first = pipeline._process(ingest_text(TICKET, "out.png"), source_path=None)
    second = pipeline._process(ingest_text("Hotel Example", "hotel.png"), source_path=None)

    assert set(combine([first, second]).source_text) == {"out.png", "hotel.png"}


def test_it_reaches_the_submission(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_STATE_DIR", str(tmp_path))
    import wayfare.config as config

    config._config = None

    itinerary = Itinerary()
    itinerary.records = [flight()]
    itinerary.source_text = {"ticket.png": TICKET}

    submission = store.commit(itinerary, "ticket.png", dry_run=True)
    assert submission.to_dict()["source_text"]["ticket.png"] == TICKET


# --- the barcode nobody was using ---------------------------------------


def test_a_barcode_that_is_not_a_boarding_pass_reaches_the_reader(monkeypatch):
    """A rail QR code is machine-written text about this exact booking."""
    seen = {}

    def capture(text, source_file, confidence, expect=None, only=None):
        seen["text"] = text
        return []

    monkeypatch.setattr(pipeline.barcode_extractor, "scan_images", lambda paths: ["RAIL-RES-8842"])
    monkeypatch.setattr(pipeline.llm_extractor, "extract", capture)

    itinerary = pipeline._process(ingest_text(TICKET, "ticket.png"), source_path=None)

    assert "RAIL-RES-8842" in seen["text"]
    assert pipeline.BARCODE_HEADING in seen["text"]
    assert any(i.code == "barcode.not_a_boarding_pass" for i in itinerary.issues)


def test_the_barcode_contents_are_quotable_as_evidence(monkeypatch):
    """Appending it to the source text is also what lets a field be quoted."""
    monkeypatch.setattr(pipeline.barcode_extractor, "scan_images", lambda paths: ["RAIL-RES-8842"])
    monkeypatch.setattr(pipeline.llm_extractor, "extract", lambda *a, **k: [])

    itinerary = pipeline._process(ingest_text(TICKET, "ticket.png"), source_path=None)
    assert "RAIL-RES-8842" in itinerary.source_text["ticket.png"]


def test_a_real_boarding_pass_is_not_treated_as_an_unknown_barcode(monkeypatch):
    payload = (
        "M1DESMARAIS/LUC       E" "ABC123 " "YUL" "FRA" "AC " "0834 " "326" "J" "004A" "00025"
        "1" "00"
    )
    monkeypatch.setattr(pipeline.barcode_extractor, "scan_images", lambda paths: [payload])
    monkeypatch.setattr(pipeline.llm_extractor, "extract", lambda *a, **k: [])

    itinerary = pipeline._process(ingest_text("", "pass.png"), source_path=None)
    assert not any(i.code == "barcode.not_a_boarding_pass" for i in itinerary.issues)


# --- the year the barcode does not have ---------------------------------


def test_a_barcode_and_the_markup_beside_it_are_one_flight():
    """A boarding pass barcode carries a day of the year and no year.

    The extractor has to assume one, and assumed this year. Compared against
    a page that states the year outright, the same flight looked like two
    flights nine years apart, and the traveller was shown it twice.
    """
    from_barcode = flight(
        departure=LocalTime(local=datetime(2026, 7, 20, 0, 0)),
        provenance=Provenance(extractor="barcode"),
    )
    from_markup = flight(
        departure=LocalTime(local=datetime(2017, 7, 20, 17, 50)),
        provenance=Provenance(extractor="structured"),
    )
    assert pipeline._same_journey(from_barcode, from_markup)


def test_a_genuinely_different_day_is_still_a_different_flight():
    """Only the year is forgiven — the barcode does know the day."""
    from_barcode = flight(
        departure=LocalTime(local=datetime(2026, 7, 20, 0, 0)),
        provenance=Provenance(extractor="barcode"),
    )
    from_markup = flight(
        departure=LocalTime(local=datetime(2017, 9, 3, 17, 50)),
        provenance=Provenance(extractor="structured"),
    )
    assert not pipeline._same_journey(from_barcode, from_markup)


def test_the_markup_supplies_the_century_the_barcode_lacks():
    """Which of the two wins, once they are known to be one flight."""
    # Through `_merge`, not `_merge_pair`: which of the two is the primary is
    # the whole question here, and it is `_merge` that decides it from the
    # trust ladder. The barcode is listed first, as the pipeline reads it
    # first, so arrival order cannot be what settles this.
    itinerary = Itinerary()
    (merged,) = pipeline._merge(
        [
            flight(
                departure=LocalTime(local=datetime(2026, 7, 20, 0, 0)),
                confirmation="YYY08",
                provenance=Provenance(extractor="barcode"),
            ),
            flight(
                departure=LocalTime(local=datetime(2017, 7, 20, 17, 50)),
                confirmation="XXX007",
                provenance=Provenance(extractor="structured"),
            ),
        ],
        itinerary,
    )
    assert merged.departure.local.year == 2017
    assert merged.confirmation == "XXX007"
