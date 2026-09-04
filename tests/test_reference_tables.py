"""Two lookups the tool was previously inferring: the airline and the station.

Neither is a judgement. Which airline flies under "S4" and which timezone
Montpellier Saint-Roch is in are facts, and asking a model for a fact spends a
call to get an invention whenever the model does not know.
"""

import pytest

from wayfare import reference


@pytest.fixture(autouse=True)
def tables(tmp_path, monkeypatch):
    """Small stand-ins, shaped exactly like the real files."""
    monkeypatch.setenv("WAYFARE_DATA_DIR", str(tmp_path))
    import wayfare.config as config

    config._config = None
    reference.clear_cache()

    (tmp_path / "airlines.csv").write_text(
        "pk^env_id^validity_from^validity_to^3char_code^2char_code^num_code^name^name2\n"
        "air-lufthansa^^1955-04-01^^DLH^LH^220^Lufthansa^\n"
        "air-lufthansa-cargo^^1977-01-01^^GEC^LH^615^Lufthansa Cargo^\n"
        "air-sata^^1990-01-01^^RZO^S4^331^SATA International^\n"
        "air-gone^^1960-01-01^2001-11-12^SAB^SN^82^Sabena^\n",
        encoding="utf-8",
    )
    (tmp_path / "stations.csv").write_text(
        "id;name;slug;uic;uic8_sncf;latitude;longitude;parent_station_id;hub_id;"
        "country;time_zone;is_city;is_main_station\n"
        "1;Montpellier St-Roch;m;8777300;87773002;43.60;3.88;;;FR;Europe/Paris;f;t\n"
        "2;Paris Gare du Nord;p;8727100;87271007;48.88;2.35;;;FR;Europe/Paris;f;t\n"
        "3;Paris Gare du Nord Eurostar;pe;;;48.88;2.35;;;FR;Europe/Paris;f;f\n"
        "4;Paris Gare de Lyon;pl;8768600;87686006;48.84;2.37;;;FR;Europe/Paris;f;t\n"
        "5;Köln Hbf;k;8000207;;50.94;6.95;;;DE;Europe/Berlin;f;t\n",
        encoding="utf-8",
    )
    yield
    reference.clear_cache()
    config._config = None


# --- airlines -----------------------------------------------------------


def test_a_code_becomes_a_name():
    assert reference.airline("S4") == "SATA International"


def test_the_passenger_airline_wins_over_the_freight_arm():
    """Looking up LH naively returns Lufthansa Cargo."""
    assert reference.airline("LH") == "Lufthansa"


def test_an_airline_that_has_stopped_flying_is_not_offered():
    assert reference.airline("SN") is None


def test_an_unknown_code_is_simply_unknown():
    assert reference.airline("ZZ") is None
    assert reference.airline(None) is None


# --- stations -----------------------------------------------------------


def test_an_operators_abbreviation_resolves():
    """SNCF prints "MONTPELLIER ST-RO"."""
    found = reference.station("MONTPELLIER ST-RO")
    assert found.name == "Montpellier St-Roch"
    assert found.timezone == "Europe/Paris"
    assert found.uic == "8777300"


def test_the_full_name_resolves_too():
    """A ticket abbreviates; a model spells out. Both have to land."""
    assert reference.station("Montpellier Saint-Roch").name == "Montpellier St-Roch"


def test_words_the_operator_drops_are_not_required():
    """"Paris Nord" is Paris Gare du Nord."""
    assert reference.station("Paris Nord").name == "Paris Gare du Nord"


def test_a_hall_inside_a_station_does_not_make_it_ambiguous():
    """Gare du Nord and its Eurostar hall both match; the station is meant."""
    assert reference.station("Paris Nord").name == "Paris Gare du Nord"


def test_two_different_termini_never_resolve_to_each_other():
    """A wrong station is worse than an unresolved one."""
    assert reference.station("Paris Gare") is None


def test_one_word_is_not_enough():
    assert reference.station("Nord") is None
    assert reference.station("Paris") is None


def test_a_station_nobody_listed_is_unresolved():
    assert reference.station("New York Penn Station") is None


def test_missing_tables_are_not_an_error(tmp_path, monkeypatch):
    """A lookup is a convenience; a document must still read without it."""
    monkeypatch.setenv("WAYFARE_DATA_DIR", str(tmp_path / "empty"))
    import wayfare.config as config

    config._config = None
    reference.clear_cache()

    assert reference.airline("LH") is None
    assert reference.station("Köln Hbf") is None
    assert reference.available() == (False, False)
