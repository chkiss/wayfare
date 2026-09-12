"""Rail tickets that print the journey as a timetable table.

České dráhy heads a block "Jízdní řád a rezervace" and lists the connection
under it, one row per station:

    Praha hl.n.            06.08. 17:50 EC 283 258   41, 42, 44 - 46
    Pardubice hl.n.        06.08. 18:46
    Pardubice hl.n.        06.08. 19:03 R 1262    1  21 - 24, 28
    Hradec Králové hl.n.   06.08. 19:23

A row that names a service is a departure; the row under it is where that
service is left. The pairs are the legs, and they are stated exactly — this is
the operator's own printout, not prose to be interpreted. Read as text it is
ambiguous enough that a model took the first leg of a three-leg ticket and
stopped, losing a train and a replacement bus.

Two details the layout forces, both visible above:

* The service can be followed by further columns — coach and seat — so only
  the first number after the operator belongs to the service. "EC 283 258" is
  train EC 283 in coach 258.
* The rows run into an unrelated right-hand column on the printed page
  ("Údaje pro kontrolu", the traveller's name, a transaction code), which
  arrives glued to the end of the line. Nothing after the seat column can be
  trusted to belong to the row.

The year is not in the table. It comes from the issue date printed elsewhere
on the ticket, which is the only full date on the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from ..schema import LocalTime, Place, Provenance, Record, TrainRecord

#: The heading that opens the table. Matched on the Czech, because the English
#: and German translations sit on the line below it and the block is the same
#: block in every one. What follows the words varies and is not anchored: a
#: ticket with no reservation heads the block "Jízdní řád" alone, and a return
#: ticket heads two tables "pro cestu TAM" and "pro cestu ZPĚT".
_HEADING = re.compile(r"^\s*Jízdní řád\b", re.M)

#: A station row: name, then "dd.mm." and "hh:mm", then optionally a service.
#: The name is taken lazily so it stops at the run of spaces before the date
#: rather than swallowing it.
_ROW = re.compile(
    r"^(?P<station>\S.*?)\s{2,}"
    r"(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?P<rest>.*)$"
)

#: The service, at the start of what follows the time. The case carries no
#: meaning at all: the same operator prints "EC", "Sp", "Bus" and "rj" on
#: tickets of the same kind, and requiring a capital lost the return half of
#: every return ticket. Length is what keeps this off the unrelated right-hand
#: column, whose words are all longer than four letters before their first
#: space.
_SERVICE = re.compile(r"^\s+(?P<operator>[A-Za-z]{1,4})\s+(?P<number>\d{1,6})\b")

#: Coach and seat, in the columns after the service.
_SEATING = re.compile(r"^\s+(?P<coach>\d{1,4})(?:\s+(?P<seat>\d[\d,\s–-]*\d|\d))?")

#: The only full date on the ticket, which is where the year comes from. The
#: table prints "dd.mm." and nothing else, so a New Year's Eve return ticket
#: would otherwise bring the traveller home eleven months before they left.
_ISSUED = re.compile(r"(?<!\d)(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})(?!\d)")

#: Where the table stops. The rows are followed by the operator's conditions
#: in prose, which contain dates and would otherwise read as more stations.
_END_OF_TABLE = re.compile(r"^\s*(?:Jízdenku lze použít|Rezervace místa platí)", re.M)


@dataclass
class _Row:
    station: str
    when: datetime
    operator: str | None
    number: str | None
    coach: str | None
    seat: str | None


def looks_like_rail_table(text: str) -> bool:
    return bool(_HEADING.search(text or ""))


def extract(text: str, source_file: str) -> list[Record]:
    """Every leg the table states, or nothing when it states none."""
    if not looks_like_rail_table(text):
        return []

    issued = _issued(text)
    if issued is None:
        # Without a year the times cannot be placed on a calendar at all, and
        # a guess would put the journey in the wrong one. Leave it to the
        # model rather than invent the missing digits.
        return []

    rows = _rows(text, issued)
    legs: list[Record] = []
    for index, row in enumerate(rows):
        if row.number is None:
            continue  # An arrival row; it belongs to the leg above it.
        arrival = rows[index + 1] if index + 1 < len(rows) else None
        if arrival is None or arrival.number is not None:
            # A departure with nothing under it, or another departure: the
            # table is not the shape this reads, so say nothing about it.
            continue
        legs.append(_leg(row, arrival, source_file))
    return legs


def _issued(text: str) -> date | None:
    match = _ISSUED.search(text)
    if not match:
        return None
    try:
        return date(
            int(match.group("year")), int(match.group("month")), int(match.group("day"))
        )
    except ValueError:
        return None


def _rows(text: str, issued: date) -> list[_Row]:
    # From the first heading to the end, not heading by heading: a return
    # ticket prints two tables, and the rows of both are rows of this journey.
    # The second heading is not a station row, so it simply does not match.
    start = _HEADING.search(text)
    block = text[start.end():] if start else text
    end = _END_OF_TABLE.search(block)
    if end:
        block = block[: end.start()]

    rows: list[_Row] = []
    previous: datetime | None = None
    for line in block.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        month, day = int(match.group("month")), int(match.group("day"))
        hour, minute = int(match.group("hour")), int(match.group("minute"))
        try:
            when = datetime(issued.year, month, day, hour, minute)
        except ValueError:
            continue

        # A journey runs forwards, and cannot be taken before the ticket was
        # bought. Either being violated means the printed "dd.mm." has crossed
        # into the following year.
        behind = when.date() < (previous.date() if previous else issued)
        if behind:
            try:
                when = when.replace(year=issued.year + 1)
            except ValueError:  # 29 February in a year that has none
                continue
        previous = when

        rest = match.group("rest")
        service = _SERVICE.match(rest)
        coach = seat = None
        operator = number = None
        if service:
            operator = service.group("operator")
            number = service.group("number")
            seating = _SEATING.match(rest[service.end():])
            if seating:
                coach = seating.group("coach")
                seat = (seating.group("seat") or "").strip() or None

        rows.append(
            _Row(
                station=match.group("station").strip(),
                when=when,
                operator=operator,
                number=number,
                coach=coach,
                seat=seat,
            )
        )
    return rows


def _leg(departure: _Row, arrival: _Row, source_file: str) -> TrainRecord:
    return TrainRecord(
        mode="train",
        operator=departure.operator,
        number=departure.number,
        origin=Place(name=departure.station),
        destination=Place(name=arrival.station),
        departure=LocalTime(local=departure.when),
        arrival=LocalTime(local=arrival.when),
        coach=departure.coach,
        seat=departure.seat,
        # The operator printed this table. Same standing as a calendar
        # attachment: read, not interpreted.
        extraction_confidence=0.9,
        provenance=Provenance(extractor="railtable", source_file=source_file),
    )
