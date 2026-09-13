"""Rail tickets that print the connection as a timetable.

Every layout here is a real České dráhy ticket from KItinerary's corpus. The
table states the journey exactly, and reading it as prose lost legs: a model
took the first leg of a three-leg ticket and stopped, dropping a train and the
replacement bus after it.
"""

from wayfare.extractors import railtable

MULTILEG = """\
Datum vystavení/datum platby: 31.7.2019 21:28            Platba:      KARTOU/CC
Jízdní řád a rezervace
Timetable and Reservations / Fahrplan und Reservierung
Stanice                             Odj. / Příj. x            w      Místo / Seat / Sitzplatz
Praha hl.n.                        06.08. 17:50 EC 283 258           41, 42, 44 - 46
Pardubice hl.n.                    06.08. 18:46
Ref: 544265457956, 545265457957                                      Údaje pro kontrolu
Pardubice hl.n.                    06.08. 19:03 R 1262        1      21 - 24, 28
Hradec Králové hl.n.               06.08. 19:23
Hradec Králové hl.n.               06.08. 19:33 Bus 501262
Jaroměř                            06.08. 19:54
Jízdenku lze použít po stejné trase i pro jiné vlakové spojení.
"""


def test_every_leg_of_a_multileg_ticket_is_read():
    first, second, third = railtable.extract(MULTILEG, "cd.txt")

    assert (first.operator, first.number) == ("EC", "283")
    assert (second.operator, second.number) == ("R", "1262")
    assert (third.operator, third.number) == ("Bus", "501262")
    assert first.origin.name == "Praha hl.n."
    assert third.destination.name == "Jaroměř"
    assert third.departure.local.hour == 19


def test_the_columns_after_the_service_are_coach_and_seat():
    """"EC 283 258" is train EC 283 in coach 258, not train number 283258."""
    first, _, third = railtable.extract(MULTILEG, "cd.txt")

    assert (first.coach, first.seat) == ("258", "41, 42, 44 - 46")
    assert (third.coach, third.seat) == (None, None)


def test_the_right_hand_column_is_not_read_as_a_station():
    """The printed page has an unrelated column that arrives glued to the row."""
    stations = {leg.origin.name for leg in railtable.extract(MULTILEG, "cd.txt")}
    assert "Ref: 544265457956, 545265457957" not in stations
    assert stations == {"Praha hl.n.", "Pardubice hl.n.", "Hradec Králové hl.n."}


RETURN = """\
Datum vystavení/datum platby: 30.12.2017 09:14
Jízdní řád a rezervace pro cestu TAM
Stanice                          Odj. / Příj. x           w       Místo / Seat / Sitzplatz
Praha hl.n.                     31.12. 13:51 EC 173 262           71
Brno hl.n.                      31.12. 16:20
Jízdní řád a rezervace pro cestu ZPĚT
Stanice                          Odj. / Příj. x           w       Místo / Seat / Sitzplatz
Brno hl.n.                      01.01. 10:38 rj 72        27      51
Praha hl.n.                     01.01. 13:07
"""


def test_a_return_ticket_prints_two_tables_and_both_are_read():
    out, back = railtable.extract(RETURN, "cd-return.txt")

    assert (out.operator, out.number) == ("EC", "173")
    # Lower case: the same operator prints "EC", "Sp", "Bus" and "rj".
    assert (back.operator, back.number) == ("rj", "72")


def test_the_new_year_is_crossed_rather_than_travelled_backwards():
    """The table prints "dd.mm." with no year.

    Taking the issue date's year for both halves brought the traveller home
    eleven months before they left.
    """
    out, back = railtable.extract(RETURN, "cd-return.txt")

    assert out.departure.local.year == 2017
    assert back.departure.local.year == 2018
    assert back.departure.local > out.arrival.local


NO_RESERVATION = """\
Datum vystavení/datum platby: 1.1.2017 08:00
Jízdní řád
Timetable / Fahrplan
Stanice                          Odj. / Příj. x           w       Místo / Seat / Sitzplatz
Lysá n.Labem                    01.01. 16:55 Os 9424                        Údaje pro kontrolu
Praha Masarykovo n.             01.01. 17:33                                Angaben für Kontrolle
Jízdenku lze použít po stejné trase i pro jiné vlakové spojení.
"""


def test_a_ticket_with_no_reservation_heads_the_table_differently():
    (leg,) = railtable.extract(NO_RESERVATION, "cd-noseat.txt")

    assert (leg.operator, leg.number) == ("Os", "9424")
    assert leg.origin.name == "Lysá n.Labem"
    assert leg.departure.local.year == 2017


GERMAN = """\
 Fahrtantritt Hinfahrt: 03.11.2027
Ihre Reiseverbindung und Reservierung Hinfahrt am 03.11.2027
Halt                                   Datum    Zeit       Gleis         Produkte    Reservierung
Berlin Hbf (tief)                      03.11.   ab 07:49   4A-D          ICE 954     1 Sitzplatz, Wg. 34, Pl. 61
Köln Hbf                               03.11.   an 12:09   6 D-G                     Nichtraucher, Res.Nr. 1234 1013
Köln Hbf                               03.11.   ab 12:17   10 A-B        S0
Somewhere(Specific)                    03.11.   an 12:45   3
"""


def test_a_different_operator_with_different_columns_is_read_too():
    """Deutsche Bahn, not České dráhy: the reason this reads shape, not words.

    The columns are in another order, the service sits past the platform
    rather than beside the time, and departures are marked "ab"/"an" instead
    of being inferred. Nothing in the module knows any of that.
    """
    first, second = railtable.extract(GERMAN, "db.txt")

    assert (first.operator, first.number) == ("ICE", "954")
    assert first.origin.name == "Berlin Hbf (tief)"
    assert first.destination.name == "Köln Hbf"
    assert first.departure.local.year == 2027

    # "S0" has no space in it and is still a service.
    assert (second.operator, second.number) == ("S", "0")


def test_the_platform_column_is_not_read_as_a_service():
    """"4A-D" and "10 A-B" sit where a service could, and are not one."""
    for leg in railtable.extract(GERMAN, "db.txt"):
        assert leg.number not in {"4", "10", "6", "3"}


def test_a_booking_reference_line_is_not_a_station():
    """A flight itinerary has lines shaped exactly like station rows.

        Date of booking:            23.04.2017  11:09
        Date of change:             23.04.2017  11:09

    Two of them in a row would pair into a journey between two labels. The
    colon is what says these are labels — this reader sits above the model in
    the trust ladder, so a confident wrong answer here overwrites a right one.
    """
    flight = (
        "MR John Doe                                  23.04.2017 | 11:10:45 Uhr\n"
        "   Date of booking:            23.04.2017  11:09\n"
        "   Date of change:             23.04.2017  11:09\n"
    )
    assert railtable.extract(flight, "eurowings.txt") == []


def test_a_document_with_no_table_is_left_alone():
    assert railtable.extract("Dear passenger, your train leaves at 10:00", "x.txt") == []
    assert not railtable.looks_like_rail_table("Jízdenka a místenka")


def test_a_table_with_no_year_anywhere_is_left_to_the_model():
    """A guessed year puts the journey in the wrong calendar entirely."""
    undated = MULTILEG.replace("31.7.2019 21:28", "someday")
    assert railtable.extract(undated, "cd.txt") == []
