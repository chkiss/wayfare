"""Giving a named airport its code, without guessing which one.

Plenty of confirmations print "London Heathrow" and never LHR. The reading is
right and the record is poorer for it: with no code there is no distance, so
no block-time check, and a duplicate already on the calendar is harder to
spot. Measured on the corpus, this was the largest remaining gap on the flight
path — origin and destination scored 41% where the flight number scored 68%.

The danger is the obvious fix. The loose lookup that resolves a timezone will
happily turn "Berlin-Tegel" into BER: same city, same zone, right answer for a
timezone and the wrong airport to print on a ticket that says TXL. Tegel closed
in 2020 and OurAirports strips the code from a closed airport, so that lookup
cannot even return the right one.
"""

import pytest

from wayfare.airports import get_airport_db


def test_a_named_airport_gets_its_code():
    assert get_airport_db().code_for_name("London Heathrow").iata == "LHR"


def test_the_full_name_works_too():
    assert get_airport_db().code_for_name("London Heathrow Airport").iata == "LHR"


def test_a_bare_city_never_becomes_an_airport():
    """"London" is five airports; picking one is a guess."""
    db = get_airport_db()
    assert db.code_for_name("London") is None
    assert db.code_for_name("Boston") is None


def test_the_words_after_the_city_are_what_decide_it():
    """This is what stops "Berlin-Tegel" resolving to Berlin Brandenburg."""
    db = get_airport_db()
    assert db.code_for_name("Frankfurt am Main").iata == "FRA"
    assert db.code_for_name("Frankfurt Hahn") is None


def test_an_airport_nobody_has_heard_of_stays_unresolved():
    assert get_airport_db().code_for_name("Berlin Tegel") is None


def test_a_loose_city_match_is_not_used():
    """`find_place` would answer this; it exists to produce a timezone, where
    the nearest large airport is good enough. A code is not."""
    db = get_airport_db()
    assert db.find_place("Boston") is not None
    assert db.code_for_name("Boston") is None
