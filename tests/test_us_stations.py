"""US stations, by the code the ticket prints.

Trainline covers Europe and nothing else, so "NYP" — the code on every Amtrak
ticket into New York, and the destination this whole line of work started with
— resolved to nothing at all.

Amtrak's own GTFS feed carries the code, the timezone and the coordinates. It
does not carry a name anyone would recognise: Penn Station is filed as "Ny
Moynihan Train Hall At Penn Station", which shares not one leading word with
what a ticket or a person calls it. So the code does the resolving and the
document keeps its name.
"""

from datetime import datetime

import pytest

from wayfare import reference
from wayfare.schema import (
    Itinerary,
    LocalTime,
    Place,
    Provenance,
    TrainRecord,
)
from wayfare.validate import resolve


@pytest.fixture(autouse=True)
def stops(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_DATA_DIR", str(tmp_path))
    import wayfare.config as config

    config._config = None
    reference.clear_cache()
    (tmp_path / "stops.csv").write_text(
        "stop_id,stop_code,stop_name,stop_url,stop_timezone,stop_lat,stop_lon\n"
        "NYP,NYP,Ny Moynihan Train Hall At Penn Station,,America/New_York,40.750327,-73.994459\n"
        "BBY,BBY,Boston,,America/New_York,42.347317,-71.075828\n"
        "CHI,CHI,Chicago Union Station,,America/Chicago,41.878766,-87.639753\n"
        "LAX,LAX,Los Angeles Union Station,,America/Los_Angeles,34.056110,-118.236176\n",
        encoding="utf-8",
    )
    yield
    reference.clear_cache()
    config._config = None


def test_the_code_on_the_ticket_resolves():
    found = reference.station_by_code("NYP")
    assert found.name == "Ny Moynihan Train Hall At Penn Station"
    assert found.timezone == "America/New_York"


def test_a_transcontinental_journey_gets_two_different_zones():
    """The reason any of this matters: an arrival written in the departure's
    timezone is three hours wrong on a calendar."""
    assert reference.station_by_code("CHI").timezone == "America/Chicago"
    assert reference.station_by_code("LAX").timezone == "America/Los_Angeles"


def test_an_unknown_code_resolves_to_nothing():
    assert reference.station_by_code("ZZZ") is None
    assert reference.station_by_code(None) is None


def test_the_document_keeps_the_name_it_printed():
    """"Penn Station" belongs in the title; Amtrak's filing name does not."""
    record = TrainRecord(
        mode="train",
        operator="Amtrak",
        number="2151",
        origin=Place(name="Back Bay Station", detail="BBY"),
        destination=Place(name="Penn Station", detail="NYP"),
        departure=LocalTime(local=datetime(2026, 9, 27, 9, 55)),
        provenance=Provenance(extractor="llm"),
    )
    resolve.run(Itinerary(records=[record]))

    assert record.destination.name == "Penn Station"
    assert record.destination.timezone == "America/New_York"
    assert record.destination.latitude == pytest.approx(40.750327)


def test_the_code_is_taken_from_wherever_the_reader_put_it():
    """A calendar reader puts a bracketed code in the detail; a model asked
    for an airport code puts it in iata."""
    record = TrainRecord(
        mode="train",
        operator="Amtrak",
        number="2151",
        origin=Place(name="Chicago", iata="CHI"),
        destination=Place(name="Los Angeles", detail="LAX"),
        departure=LocalTime(local=datetime(2026, 9, 27, 9, 55)),
        provenance=Provenance(extractor="llm"),
    )
    resolve.run(Itinerary(records=[record]))
    assert record.destination.timezone == "America/Los_Angeles"


def test_europe_still_works_without_the_us_table(tmp_path, monkeypatch):
    """The two tables are independent; neither is required for the other."""
    monkeypatch.setenv("WAYFARE_DATA_DIR", str(tmp_path / "eu-only"))
    import wayfare.config as config

    config._config = None
    reference.clear_cache()
    (tmp_path / "eu-only").mkdir()
    (tmp_path / "eu-only" / "stations.csv").write_text(
        "id;name;slug;uic;uic8_sncf;latitude;longitude;parent_station_id;hub_id;"
        "country;time_zone;is_city;is_main_station\n"
        "1;Köln Hbf;k;8000207;;50.94;6.95;;;DE;Europe/Berlin;f;t\n",
        encoding="utf-8",
    )
    assert reference.station("Köln Hbf").timezone == "Europe/Berlin"
    assert reference.station_by_code("NYP") is None
