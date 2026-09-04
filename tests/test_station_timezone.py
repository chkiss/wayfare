"""A rail timezone from the station, not from a guessed city.

Guessing a city from a station name and then an airport from the city is two
inferences deep. "Gare de Lyon is in Paris" is the easy case, and the one the
guesswork was built for. "MONTPELLIER ST-RO" is not.
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
def stations(tmp_path, monkeypatch):
    monkeypatch.setenv("WAYFARE_DATA_DIR", str(tmp_path))
    import wayfare.config as config

    config._config = None
    reference.clear_cache()
    (tmp_path / "stations.csv").write_text(
        "id;name;slug;uic;uic8_sncf;latitude;longitude;parent_station_id;hub_id;"
        "country;time_zone;is_city;is_main_station\n"
        "1;Montpellier St-Roch;m;8777300;87773002;43.60;3.88;;;FR;Europe/Paris;f;t\n"
        "2;Köln Hbf;k;8000207;;50.94;6.95;;;DE;Europe/Berlin;f;t\n",
        encoding="utf-8",
    )
    yield
    reference.clear_cache()
    config._config = None


def leg(origin, destination):
    return TrainRecord(
        mode="train",
        operator="SNCF",
        number="6857",
        origin=Place(name=origin),
        destination=Place(name=destination),
        departure=LocalTime(local=datetime(2026, 7, 15, 17, 50)),
        arrival=LocalTime(local=datetime(2026, 7, 15, 19, 58)),
        provenance=Provenance(extractor="ics"),
    )


def test_the_station_gives_its_own_timezone():
    record = leg("MONTPELLIER ST-RO", "Köln Hbf")
    resolve.run(Itinerary(records=[record]))

    assert record.origin.timezone == "Europe/Paris"
    assert record.destination.timezone == "Europe/Berlin"


def test_the_departure_carries_that_zone():
    record = leg("MONTPELLIER ST-RO", "Köln Hbf")
    resolve.run(Itinerary(records=[record]))
    assert record.departure.timezone == "Europe/Paris"


def test_the_coordinates_come_with_it():
    """Which is what lets a distance check run on a railway journey at all."""
    record = leg("MONTPELLIER ST-RO", "Köln Hbf")
    resolve.run(Itinerary(records=[record]))
    assert record.origin.latitude == pytest.approx(43.60)


def test_the_resolution_is_reported():
    record = leg("MONTPELLIER ST-RO", "Köln Hbf")
    resolve.run(Itinerary(records=[record]))
    messages = [i.message for i in record.issues if i.code == "place.station_resolved"]
    assert any("Montpellier St-Roch" in m and "8777300" in m for m in messages)
    assert any("Köln Hbf" in m for m in messages)


def test_a_station_the_table_does_not_know_falls_back():
    """The city guesswork is still there for everything the table misses."""
    record = leg("Somewhere Unlisted Halt", "Köln Hbf")
    resolve.run(Itinerary(records=[record]))
    assert record.destination.timezone == "Europe/Berlin"
