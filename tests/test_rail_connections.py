"""Two trains on one afternoon are two entries, not one.

Any two ground-transport records on the same date were treated as the same
booking and merged. A Frankfurt-Köln-Paris journey is exactly that shape, so
the connection vanished and the surviving record ended where it started. The
rule had always been stricter for flights; ground transport had been getting
the loose one.
"""

from datetime import datetime

from wayfare.pipeline import _same_journey
from wayfare.schema import LocalTime, Place, Provenance, TrainRecord


def leg(number, hour, origin, destination, operator="ICE"):
    return TrainRecord(
        mode="train",
        operator=operator,
        number=number,
        origin=Place(name=origin),
        destination=Place(name=destination),
        departure=LocalTime(local=datetime(2022, 7, 26, hour, 0), timezone="Europe/Berlin"),
        provenance=Provenance(extractor="ics"),
    )


def test_two_legs_of_one_journey_stay_separate():
    first = leg("16", 10, "Frankfurt(Main)Hbf", "Köln Hbf")
    second = leg("9448", 12, "Köln Hbf", "Paris Nord", operator="THA")
    assert not _same_journey(first, second)


def test_the_same_leg_read_twice_is_still_one():
    assert _same_journey(leg("16", 10, "Frankfurt", "Köln"), leg("16", 10, "Frankfurt", "Köln"))


def test_a_leading_zero_does_not_split_a_train():
    assert _same_journey(leg("0090027", 10, "A", "B"), leg("90027", 10, "A", "B"))


def test_numberless_legs_are_told_apart_by_their_route():
    first = leg(None, 10, "Hamburg Hbf", "Münster(Westf)Hbf")
    second = leg(None, 15, "Münster(Westf)Hbf", "Dortmund Hbf")
    assert not _same_journey(first, second)


def test_numberless_legs_on_the_same_route_and_minute_are_one():
    first = leg(None, 10, "Hamburg Hbf", "Münster(Westf)Hbf")
    second = leg(None, 10, "Hamburg Hbf", "Münster(Westf)Hbf")
    assert _same_journey(first, second)


def test_different_days_are_never_the_same_journey():
    first = leg("16", 10, "Frankfurt", "Köln")
    second = leg("16", 10, "Frankfurt", "Köln")
    second.departure = LocalTime(local=datetime(2022, 7, 27, 10, 0), timezone="Europe/Berlin")
    assert not _same_journey(first, second)
