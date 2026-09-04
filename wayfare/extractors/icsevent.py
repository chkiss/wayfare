"""Reading a calendar attachment, which already says what the model was guessing.

Operators attach a `.ics` to confirmations — Deutsche Bahn, Lufthansa and
National Rail all do — and it is the least ambiguous document in the whole
pipeline: an exact start, an exact end, and a named timezone, written by the
operator's own system rather than laid out for a human eye. wayfare had a
parser for the format and used it only to learn title conventions from a
calendar export. An uploaded `.ics` went to a language model as raw text.

Measured on KItinerary's corpus, that is 14 of 50 documents and 34 of 110
legs: a quarter of the corpus handed to the least reliable reader available,
with the answer written plainly in the file.

Three layouts appear, and the differences are only in where each field hides:

* **HAFAS** (Deutsche Bahn and the operators using their system) — one event
  for the whole journey, with the individual legs listed in the description as
  "ab 10:26 Frankfurt(Main)Hbf - Gleis 19 (ICE 16)". The event alone would say
  Frankfurt to Paris; the description says that is two trains with a change at
  Köln, which is what belongs on a calendar.
* **Airline** — "Your flight from FRA to EWR", with the flight number and
  booking reference labelled in the description.
* **Journey summary** — "Glasgow Central (GLC) to London Kings Cross (KGX)",
  times and nothing else.

Everything here is deterministic. Nothing in this module guesses, and where a
field is not stated it is left empty for another extractor to fill.
"""

from __future__ import annotations

import re
from datetime import datetime

from ..icsparse import parse as parse_ics
from ..schema import (
    FlightRecord,
    LocalTime,
    Place,
    Provenance,
    Record,
    TrainRecord,
)

SOURCE = "ics"

#: Enough of a VCALENDAR to be worth parsing.
_MARKER = re.compile(r"BEGIN:VCALENDAR", re.I)

#: "A -> B", "A → B", "from A to B", "A to B". The arrow forms are tried first
#: because "to" appears inside station names ("Toronto") and an arrow does not.
_ROUTES = (
    re.compile(r"^(?P<from>.+?)\s*(?:->|→|=>|–>)\s*(?P<to>.+?)$"),
    re.compile(r"\bfrom\s+(?P<from>.+?)\s+to\s+(?P<to>.+?)$", re.I),
    re.compile(r"^(?:[^:]*:)?\s*(?P<from>.+?)\s+to\s+(?P<to>.+?)$", re.I),
)

#: A code in brackets after a station name: "Glasgow Central (GLC)".
_CODE = re.compile(r"^(?P<name>.+?)\s*\((?P<code>[A-Z0-9]{3,4})\)\s*$")

#: A booking reference, however the operator labels it.
_CONFIRMATION = re.compile(
    r"(?:booking reference|booking ref|reservation number|confirmation(?: number| code)?"
    r"|buchungsnummer|auftragsnummer|r[ée]f[ée]rence)\s*[:#]?\s*"
    r"(?P<value>[A-Z0-9]{5,8})\b",
    re.I,
)

#: "Flight number: LH 123", "Flugnummer LH123".
_FLIGHT_NUMBER = re.compile(
    r"(?:flight\s*(?:number|no\.?)|flugnummer)\s*[:#]?\s*(?P<carrier>[A-Z][A-Z0-9])\s*"
    r"(?P<number>\d{1,4})\b",
    re.I,
)

#: One HAFAS leg boundary: "ab 10:26 Frankfurt(Main)Hbf - Gleis 19 (ICE   16)"
#: or the English "dep 10:26 ...". The service in trailing brackets is
#: optional, because a regional leg often has none.
#: The words differ by language — "ab/an" in German, "dep/arr" in English,
#: "de/a" in Spanish, "fra/til" in Danish — and Deutsche Bahn sends the
#: traveller's own. Enumerating them means guessing at languages nobody here
#: can check, so the word is not matched at all: whatever begins the first of
#: these lines is this document's departure marker, and the rest follow from
#: that. The shape is the same in every language.
_HAFAS = re.compile(
    r"^\s*(?P<kind>[^\W\d_]{1,5})\s+(?P<time>\d{1,2}:\d{2})\s+(?P<place>.+?)\s*$",
)
_SERVICE = re.compile(r"\((?P<operator>[A-Z]{2,4})\s+(?P<number>\d{1,5})\)\s*$")
#: The platform, which every language writes differently — "Gleis 19", "Vía
#: 14", "Spor 6D-E", "Voie 3" — but always as a trailing " - <word> <number>"
#: after the station. Matched on that shape rather than on a list of words,
#: and only when it carries a digit, so a station whose name genuinely
#: contains a dash keeps it.
_PLATFORM = re.compile(r"\s+-\s+(?=[^-]*\d)\S.*$")

#: Where a station name stops and the operator's extra notes begin.
_TRAILING = re.compile(r",\s*(?:seat|sitz|place|dep|arr|ab|an|coach|wagen)\b|\s+-\s+Gleis\b", re.I)

#: Words that say which kind of journey. Checked against the whole event,
#: because the operator names itself even when the summary does not
#: ("Train Company: Avanti West Coast").
_AIR_WORDS = re.compile(r"\b(flight|flug|vol|airline|airways|airport|boarding)\b", re.I)
_RAIL_WORDS = re.compile(r"\b(train|zug|rail|bahn|gleis|platform|coach|bus|station)\b", re.I)

#: Words that mean this event is a journey rather than a meeting.
_TRAVEL_WORDS = re.compile(
    r"\b(flight|flug|vol|journey|reise|fahrt|train|zug|travel|itinerary|departure)\b", re.I
)


def looks_like_calendar(text: str) -> bool:
    return bool(_MARKER.search(text or ""))


def extract(text: str, source_file: str = "-") -> list[Record]:
    """Every travel record the calendar file states."""
    records: list[Record] = []
    for event in parse_ics(text or ""):
        records.extend(_from_event(event, source_file))
    return records


def _provenance(source_file: str) -> Provenance:
    return Provenance(extractor=SOURCE, source_file=source_file)


def _local(stamp, zone: str | None, place: Place | None = None) -> LocalTime | None:
    """The clock time a traveller reads, with the zone it belongs to.

    Airlines write the stamp in UTC. That names the right instant but the
    wrong number: a Frankfurt departure stated as 13:13Z is 14:13 on the
    board, and the board is what a calendar entry has to agree with. Where the
    place is known the instant is converted to its zone; where it is not, UTC
    is kept, which is correct as an instant and honest about the rest.
    """
    if not isinstance(stamp, datetime):
        return None
    if zone not in {"UTC", "Z"} and stamp.tzinfo is None:
        return LocalTime(local=stamp, timezone=zone)

    target = _zone_of(place)
    if target is None:
        return LocalTime(local=stamp.replace(tzinfo=None), timezone="UTC")

    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo

    try:
        aware = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=_tz.utc)
        moved = aware.astimezone(ZoneInfo(target))
    except Exception:  # noqa: BLE001 - an unknown zone name is not a reason to lose the time
        return LocalTime(local=stamp.replace(tzinfo=None), timezone="UTC")
    return LocalTime(local=moved.replace(tzinfo=None), timezone=target)


def _zone_of(place: Place | None) -> str | None:
    if place is None or not place.iata:
        return None
    from ..airports import get_airport_db

    db = get_airport_db()
    return db.timezone_for(place.iata) if db.available else None


def _place(label: str | None, flight: bool = False) -> Place:
    """A station or airport as written, with a bracketed code split out."""
    text = (label or "").strip().strip(",")
    if not text:
        return Place()
    # A bare code, as an airline writes it: "Your flight from FRA to EWR".
    if flight and len(text) == 3 and text.isalpha() and text.isupper():
        if _is_airport(text):
            return Place(iata=text)

    match = _CODE.match(text)
    if not match:
        return Place(name=text)

    code, name = match.group("code"), match.group("name").strip()
    # A three-letter code in brackets is not automatically IATA, and looking it
    # up does not settle it either: National Rail writes "Glasgow Central
    # (GLC)", and GLC is Geladi Airport in Ethiopia. The database agreed, and
    # a Glasgow-to-London train became a flight to the Horn of Africa.
    #
    # So the document has to say it is a flight first. Only then is a code read
    # as IATA; otherwise it is the operator's own station reference and goes in
    # the detail, where the reader still sees it without it claiming to be an
    # airport.
    if flight and len(code) == 3 and code.isalpha() and _is_airport(code):
        return Place(iata=code, name=name)
    return Place(name=name, detail=code)


def _is_airport(code: str) -> bool:
    from ..airports import get_airport_db

    db = get_airport_db()
    return bool(db.available and db.get(code) is not None)


def _route(summary: str, flight: bool = False) -> tuple[Place, Place] | None:
    for pattern in _ROUTES:
        match = pattern.search(summary or "")
        if not match:
            continue
        origin, destination = match.group("from"), match.group("to")
        # "Journey Details: A to B" — drop a leading label.
        origin = origin.split(":")[-1].strip()
        # Operators append extras to the route line: ", seat: 1/42", ", dep
        # 12:40". The station name ends where that begins.
        destination = _TRAILING.split(destination)[0].strip()
        origin = _TRAILING.split(origin)[0].strip()
        if origin and destination:
            return _place(origin, flight), _place(destination, flight)
    return None


def _first(pattern: re.Pattern, text: str, *groups: str):
    match = pattern.search(text or "")
    if not match:
        return None if len(groups) == 1 else (None,) * len(groups)
    if len(groups) == 1:
        return match.group(groups[0])
    return tuple(match.group(g) for g in groups)


def _from_event(event, source_file: str) -> list[Record]:
    if event.all_day or not isinstance(event.start, datetime):
        return []

    legs = _hafas_legs(event, source_file)
    if legs:
        return legs

    text = f"{event.summary}\n{event.description}"
    if not _TRAVEL_WORDS.search(text) and "->" not in event.summary and "→" not in event.summary:
        return []  # An ordinary appointment that happens to be in the file.

    # What kind of journey this is has to be settled before the places are
    # read, because it decides whether a bracketed code is an airport.
    carrier, number = _first(_FLIGHT_NUMBER, text, "carrier", "number")
    is_flight = bool(carrier) or bool(_AIR_WORDS.search(text))
    if _RAIL_WORDS.search(text) and not carrier:
        is_flight = False

    route = _route(event.summary, is_flight) or _route(
        event.description.splitlines()[0] if event.description else "", is_flight
    )
    if route is None:
        return []
    origin, destination = route

    confirmation = _first(_CONFIRMATION, text, "value")
    start = _local(event.start, event.start_tz, origin)
    end = _local(event.end, event.start_tz, destination)
    if start is None:
        return []

    common = dict(
        confirmation=confirmation,
        departure=start,
        arrival=end,
        # Every field here was read from a machine-written file, so this is as
        # confident as extraction gets short of a barcode.
        extraction_confidence=0.9,
        provenance=_provenance(source_file),
    )

    if is_flight:
        return [
            FlightRecord(
                carrier=(carrier or "").upper() or None,
                number=number,
                origin=origin,
                destination=destination,
                **common,
            )
        ]

    service = _first(_SERVICE, text, "operator", "number")
    return [
        TrainRecord(
            mode="train",
            operator=service[0],
            number=service[1],
            origin=origin,
            destination=destination,
            **common,
        )
    ]


def _hafas_legs(event, source_file: str) -> list[Record]:
    """Split a HAFAS journey into the trains it is actually made of.

    The event says Frankfurt to Paris. The description says that is an ICE to
    Köln and a Thalys onward, which is two entries on a calendar and two
    chances to be on the wrong platform. Pairing is strictly departure then
    arrival, in order, so a malformed block yields nothing rather than a leg
    stitched from two different trains.
    """
    if not event.description:
        return []

    marks: list[tuple[str, str, str, str | None, str | None]] = []
    for line in event.description.splitlines():
        match = _HAFAS.match(line)
        if not match:
            continue
        place = match.group("place")
        service = _SERVICE.search(place)
        operator, number = (service.group("operator"), service.group("number")) if service else (None, None)
        if service:
            place = place[: service.start()]
        place = _PLATFORM.sub("", place).strip()
        marks.append((match.group("kind").casefold(), match.group("time"), place, operator, number))

    # The first line of a journey is a departure, so its word is what this
    # document uses for one. Every other word is an arrival.
    if marks:
        departure_word = marks[0][0]
        marks = [
            ("dep" if word == departure_word else "arr", *rest) for word, *rest in marks
        ]

    if len(marks) < 2:
        return []

    day = event.start.date()
    last_hour = -1
    records: list[Record] = []

    index = 0
    while index < len(marks) - 1:
        depart, arrive = marks[index], marks[index + 1]
        if depart[0] != "dep" or arrive[0] != "arr":
            index += 1
            continue

        times = []
        for mark in (depart, arrive):
            hour, minute = (int(part) for part in mark[1].split(":"))
            # The description prints clock times with no date. A journey runs
            # forwards, so an hour earlier than the one before it is the next
            # day — which is how an overnight leg keeps its date.
            nonlocal_day = day
            if hour * 60 + minute < last_hour:
                day = day.fromordinal(day.toordinal() + 1)
                nonlocal_day = day
            last_hour = hour * 60 + minute
            times.append(datetime(nonlocal_day.year, nonlocal_day.month, nonlocal_day.day, hour, minute))

        records.append(
            TrainRecord(
                mode="train",
                operator=depart[3],
                number=depart[4],
                origin=_place(depart[2]),
                destination=_place(arrive[2]),
                departure=LocalTime(local=times[0], timezone=event.start_tz),
                arrival=LocalTime(local=times[1], timezone=event.start_tz),
                confirmation=_first(_CONFIRMATION, event.description, "value"),
                extraction_confidence=0.9,
                provenance=_provenance(source_file),
            )
        )
        index += 2

    return records
