"""Tickets that print the journey as a timetable, whoever printed it.

A great many operators lay the connection out as a table of station rows —
a place, then a date and a time, then whatever else that operator puts in its
columns. České dráhy:

    Praha hl.n.            06.08. 17:50 EC 283 258   41, 42, 44 - 46
    Pardubice hl.n.        06.08. 18:46

Deutsche Bahn, same shape, different columns and an explicit marker:

    Berlin Hbf (tief)      03.11.   ab 07:49   4A-D    ICE 954   1 Sitzplatz…
    Köln Hbf               03.11.   an 12:09   6 D-G

This module reads the shape, not the operator. Nothing here names a language,
a country or a carrier, because the first version of it did — it was anchored
on the Czech words "Jízdní řád" and so could never fire for anybody else,
which is a way of solving one ticket rather than the problem.

What varies between operators, and is therefore parsed rather than assumed:

* **Separators.** "06.08.", "15/07", "2.7.17", "25-08-2012"; "17:50", "17h50".
* **Which row is a departure.** Three rules, tried in order, because operators
  supply the answer in three different ways: an explicit marker word before
  the time ("ab"/"an", "odj."/"příj."); failing that, a named service, since
  a row naming a train is the row that boards it; failing that, the order the
  rows are in.
* **Where the service sits.** Somewhere after the time — immediately after it
  on a Czech ticket, past the platform column on a German one.

What does *not* vary, and is what makes this readable at all: a station row
names one place and one time, and the rows pair up into legs.

Two guards earn their place. A row whose "place" ends in a colon is a label,
not a station — "Date of booking:   23.04.2017  11:09" on a flight itinerary
has exactly the shape of a station row otherwise. And a service code is one to
four letters: that alone is what keeps "VOITURE 13" and "PLACE 31" on an SNCF
ticket from being read as trains.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from ..schema import LocalTime, Place, Provenance, Record, TrainRecord

#: A candidate row: something, a wide gap, then the rest of the columns. The
#: gap is what separates a table from a sentence.
_ROW = re.compile(r"^\s*(?P<place>\S.*?)\s{2,}(?P<rest>\S.*?)\s*$")

#: A time, with either separator. "17:50" and "17h50" are the same clock.
_TIME = re.compile(r"(?<![\d:h])(?P<hour>\d{1,2})[:h](?P<minute>\d{2})(?![\d:h])")

#: A date with no year, or with one. The trailing dot is Czech and German
#: shorthand for "of this month" and belongs to the date, not the next column.
_DATE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})[./-](?P<month>\d{1,2})(?:[./-](?P<year>\d{2,4}))?\.?(?!\d)"
)

#: A full date anywhere in the document, which is where a yearless row gets
#: its year.
_FULL_DATE = re.compile(r"(?<!\d)(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{4})(?!\d)")

#: The word immediately before the time, when there is one. Which word it is
#: does not matter and is never checked against a list — only that departures
#: and arrivals use different ones.
_MARKER = re.compile(r"(?<![^\W\d_])(?P<word>[^\W\d_]{1,5}\.?)\s+(?=\d{1,2}[:h]\d{2})")

#: A service: one to four letters and a number, with or without a space
#: between them, so "ICE 954", "EC 283", "rj 72" and "S0" all read. The length
#: limit is deliberate — it is what distinguishes a service code from a column
#: of prose, and from "VOITURE 13".
_SERVICE = re.compile(
    r"(?<![A-Za-z0-9])(?P<operator>[A-Za-z]{1,4})\s*(?P<number>\d{1,6})(?![A-Za-z0-9])"
)

#: Coach and seat, where the operator puts them right after the service.
_SEATING = re.compile(r"^\s+(?P<coach>\d{1,4})(?:\s+(?P<seat>\d[\d,\s–-]*\d|\d))?")

#: A label, not a station. "Date of booking:" has a station row's shape.
_IS_LABEL = re.compile(r":\s*$")

#: A place has to contain a letter. A column of figures is not a station.
_HAS_LETTER = re.compile(r"[^\W\d_]")


@dataclass
class _Row:
    place: str
    when: datetime
    marker: str | None
    operator: str | None
    number: str | None
    coach: str | None
    seat: str | None


def looks_like_rail_table(text: str) -> bool:
    """Does this document contain a run of station rows at all?"""
    return len(_candidate_rows(text or "")) >= 2


def extract(text: str, source_file: str) -> list[Record]:
    """Every leg the table states, or nothing when it states none."""
    anchor = _anchor_year(text)
    if anchor is None:
        # Without a year the times cannot be placed on a calendar, and a guess
        # puts the journey in the wrong one. Leave it to the model rather than
        # invent the missing digits.
        return []

    rows = _rows(text, anchor)
    if len(rows) < 2:
        return []

    pairs = _pair(rows)
    legs: list[Record] = []
    for departure, arrival in pairs:
        if departure.place == arrival.place:
            continue  # A leg that ends where it starts was mispaired.
        if arrival.when < departure.when:
            continue  # So was one that arrives before it leaves.
        legs.append(_leg(departure, arrival, source_file))
    return legs


# --- finding the rows ---------------------------------------------------


def _candidate_rows(text: str) -> list[re.Match]:
    found = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        place = match.group("place")
        if _IS_LABEL.search(place) or not _HAS_LETTER.search(place):
            continue
        if not _TIME.search(match.group("rest")):
            continue
        found.append(match)
    return found


def _anchor_year(text: str) -> date | None:
    """The first full date on the ticket, which fixes the year for the rest."""
    match = _FULL_DATE.search(text)
    if not match:
        return None
    try:
        return date(
            int(match.group("year")), int(match.group("month")), int(match.group("day"))
        )
    except ValueError:
        return None


def _rows(text: str, anchor: date) -> list[_Row]:
    rows: list[_Row] = []
    previous: datetime | None = None

    for match in _candidate_rows(text):
        rest = match.group("rest")
        time = _TIME.search(rest)
        before, after = rest[: time.start()], rest[time.end():]

        day = _DATE.search(before)
        marker = _MARKER.search(rest[: time.start()])
        try:
            when = _when(day, time, anchor)
        except ValueError:
            continue

        # A journey runs forwards, and cannot be taken before the ticket was
        # issued. Either being violated means a yearless date has crossed into
        # the following year — a New Year's Eve return ticket was otherwise
        # bringing the traveller home eleven months before they left.
        if day is None or not day.group("year"):
            floor = previous.date() if previous else anchor
            if when.date() < floor:
                try:
                    when = when.replace(year=when.year + 1)
                except ValueError:  # 29 February in a year without one
                    continue
        previous = when

        service = _SERVICE.search(after)
        coach = seat = None
        if service:
            seating = _SEATING.match(after[service.end():])
            if seating:
                coach = seating.group("coach")
                seat = (seating.group("seat") or "").strip() or None

        rows.append(
            _Row(
                place=match.group("place").strip(),
                when=when,
                marker=marker.group("word").casefold() if marker else None,
                operator=service.group("operator") if service else None,
                number=service.group("number") if service else None,
                coach=coach,
                seat=seat,
            )
        )
    return rows


def _when(day, time, anchor: date) -> datetime:
    hour, minute = int(time.group("hour")), int(time.group("minute"))
    if day is None:
        return datetime(anchor.year, anchor.month, anchor.day, hour, minute)

    year = day.group("year")
    if year:
        year = int(year)
        if year < 100:  # "2.7.17"
            year += 2000
    else:
        year = anchor.year
    return datetime(year, int(day.group("month")), int(day.group("day")), hour, minute)


# --- pairing the rows into legs -----------------------------------------


def _pair(rows: list[_Row]) -> list[tuple[_Row, _Row]]:
    """Which rows are departures, and what each one arrives at.

    Operators answer this three different ways, so the rules are tried in the
    order of how much the document actually tells us.
    """
    markers = [row.marker for row in rows if row.marker]
    if len(set(markers)) >= 2:
        return _pair_by_marker(rows)
    if any(row.number for row in rows):
        return _pair_by_service(rows)
    return _pair_in_order(rows)


def _pair_by_marker(rows: list[_Row]) -> list[tuple[_Row, _Row]]:
    """"ab"/"an", "odj."/"příj.": the operator has labelled them.

    Which word means which is not looked up — whatever begins the first row is
    this document's departure marker, the same rule the calendar reader uses,
    and it holds in every language without being taught any of them.
    """
    departure_marker = next((row.marker for row in rows if row.marker), None)
    pairs = []
    for index, row in enumerate(rows):
        if row.marker != departure_marker:
            continue
        arrival = next(
            (other for other in rows[index + 1:] if other.marker and other.marker != departure_marker),
            None,
        )
        if arrival is not None:
            pairs.append((row, arrival))
    return pairs


def _pair_by_service(rows: list[_Row]) -> list[tuple[_Row, _Row]]:
    """A row that names a train is the row that boards it."""
    pairs = []
    for index, row in enumerate(rows):
        if not row.number or index + 1 >= len(rows):
            continue
        arrival = rows[index + 1]
        if arrival.number:
            continue  # Another departure: the table is not this shape.
        pairs.append((row, arrival))
    return pairs


def _pair_in_order(rows: list[_Row]) -> list[tuple[_Row, _Row]]:
    """Nothing marks them, so take them two at a time.

    Only for an even run: an odd one means at least one row is something this
    has misread, and pairing the rest would propagate the error rather than
    stop at it.
    """
    if len(rows) % 2:
        return []
    return [(rows[i], rows[i + 1]) for i in range(0, len(rows), 2)]


def _leg(departure: _Row, arrival: _Row, source_file: str) -> TrainRecord:
    return TrainRecord(
        mode="train",
        operator=departure.operator,
        number=departure.number,
        origin=Place(name=departure.place),
        destination=Place(name=arrival.place),
        departure=LocalTime(local=departure.when),
        arrival=LocalTime(local=arrival.when),
        coach=departure.coach,
        seat=departure.seat,
        # The operator printed this table. Same standing as a calendar
        # attachment: read, not interpreted.
        extraction_confidence=0.9,
        provenance=Provenance(extractor="railtable", source_file=source_file),
    )
