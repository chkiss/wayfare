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


def test_the_operator_inside_the_number_is_the_same_train():
    """Two extractors write one train two ways.

    The timetable printed on a ticket has its own operator column, so it reads
    "EC" and "283" separately; a model reading the same line answers "EC 283".
    Compared as raw strings those looked like different trains, and every leg
    of a Czech ticket survived twice — one leg became two, two became four.
    """
    from_table = leg("283", 17, "Praha hl.n.", "Pardubice hl.n.", operator="EC")
    from_model = leg("EC 283", 17, "Praha hl.n.", "Pardubice hl.n.", operator="EC")
    assert _same_journey(from_table, from_model)

    # And where the model does not separate the operator out at all.
    unsplit = leg("EC 283", 17, "Praha hl.n.", "Pardubice hl.n.", operator=None)
    assert _same_journey(from_table, unsplit)


def test_a_company_name_does_not_make_it_a_different_train():
    """Extractors disagree about what `operator` holds.

    The calendar attachment puts the service code there, a model reading the
    same journey puts the company. Treating those as rival codes said every
    leg was a different train and doubled the whole corpus — calendar files
    included, which the Czech-only doubling had at least spared.
    """
    from_calendar = leg("1689", 22, "Hamburg Hbf", "Hannover Hbf", operator="ICE")
    from_model = leg("ICE 1689", 22, "Hamburg Hbf", "Hannover Hbf", operator="Deutsche Bahn")
    assert _same_journey(from_calendar, from_model)


def test_the_operator_is_not_used_to_tell_two_records_apart():
    """A deliberate trade, and the second thing this file got wrong.

    Rejecting a merge when the operators differ looks obviously right — "EC
    283" and "R 283" really are different trains. But extractors do not agree
    on what `operator` holds: reading one Danish itinerary the calendar
    attachment puts the service code there ("X2", "R", "IC") and the model
    puts the carrier ("SJ", "DSB", "DB"). Both are two to four letters, so no
    test separates them, and the rule refused to merge legs that were the same
    train — seven duplicates on a corpus of thirty-two.

    So two trains sharing a number on one day will merge if that ever happens.
    That is rarer, and quieter, than showing the traveller the same train
    twice.
    """
    assert _same_journey(
        leg("283", 17, "A", "B", operator="EC"),
        leg("283", 17, "A", "B", operator="R"),
    )


def test_different_days_are_never_the_same_journey():
    first = leg("16", 10, "Frankfurt", "Köln")
    second = leg("16", 10, "Frankfurt", "Köln")
    second.departure = LocalTime(local=datetime(2022, 7, 27, 10, 0), timezone="Europe/Berlin")
    assert not _same_journey(first, second)
