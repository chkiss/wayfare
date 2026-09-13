"""schema.org reservations, published by the operator in the page itself.

Airlines, hotels and booking sites embed their confirmations as structured
data — `FlightReservation`, `LodgingReservation`, `TrainReservation` — in a
`<script type="application/ld+json">` block. Google standardised it for
confirmation emails, and the vocabulary is the one wayfare's own schema
mirrors, field for field.

This is the extractor that does not have to be generalised, because it was
never specific: it reads a published standard rather than an operator's
layout. One reader serves every carrier that emits it, including carriers
nobody here has heard of, and it will not drift when a carrier redesigns its
ticket. Every other deterministic reader in this package is an attempt to
recover, from ink and column positions, what this one is simply handed.

Where a document offers it, nothing else should be guessing: the operator's
own machine-written statement of the booking outranks anything read off the
page, which is why these records arrive with the same standing as a calendar
attachment.

Only reservations are taken. A page also carrying `Hotel`, `Event` or
`Product` markup is describing something, not confirming a booking, and a
description is not an itinerary.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Iterable

from ..schema import (
    FlightRecord,
    LocalTime,
    LodgingRecord,
    Place,
    Provenance,
    Record,
    TrainRecord,
)

#: The block the standard puts it in. Attributes vary in order and quoting, so
#: the type is matched wherever it sits in the tag.
_SCRIPT = re.compile(
    r"<script\b[^>]*\btype\s*=\s*[\"']application/ld\+json[\"'][^>]*>(?P<body>.*?)</script>",
    re.I | re.S,
)

#: What each reservation type becomes here. Anything absent is not a booking.
_RESERVATIONS = {
    "flightreservation": "flight",
    "trainreservation": "train",
    "busreservation": "bus",
    "lodgingreservation": "lodging",
}


#: The same vocabulary spelled as HTML attributes instead of a JSON block.
_HAS_MICRODATA = re.compile(r"\bitemscope\b", re.I)


def looks_like_structured(text: str) -> bool:
    text = text or ""
    return bool(_SCRIPT.search(text) or _HAS_MICRODATA.search(text))


def extract(text: str, source_file: str) -> list[Record]:
    """Every reservation the page states about itself.

    Both spellings of the one standard are read and the results pooled.
    A page carrying each — some publish the JSON block and mark the visible
    HTML up as well — states its booking twice, and the pipeline merges the
    duplicate the same way it merges any two readings of one journey.
    """
    text = text or ""
    entries = _reservations(text) + _microdata_reservations(text)

    records: list[Record] = []
    for entry in entries:
        record = _record(entry, source_file)
        if record is not None:
            records.append(record)
    return records


# --- finding the markup -------------------------------------------------


def _reservations(text: str) -> list[dict]:
    found: list[dict] = []
    for block in _SCRIPT.finditer(text):
        payload = _parse(block.group("body"))
        for entry in _walk(payload):
            kind = _RESERVATIONS.get(str(entry.get("@type", "")).casefold())
            if kind:
                found.append(entry)
    return found


def _parse(body: str) -> Any:
    """Parse one block, tolerating what publishers actually ship.

    A broken block is skipped rather than fatal: a page carrying two
    reservations and one malformed advertisement should yield two
    reservations, and refusing the whole page would lose a real booking to
    somebody else's markup error.
    """
    try:
        return json.loads(body)
    except ValueError:
        pass

    cleaned = body.strip().rstrip(";")
    # Trailing commas before a close.
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    # Two objects juxtaposed with no comma between them, which is what an
    # array assembled by string concatenation looks like when the separator is
    # forgotten. One airline ships both its flights in one such block, and
    # refusing it loses a real booking to a missing character.
    cleaned = re.sub(r"}\s*{", "},{", cleaned)
    try:
        return json.loads(cleaned)
    except ValueError:
        return None


def _walk(payload: Any) -> Iterable[dict]:
    """Every object in the payload, however it is nested.

    Publishers ship a bare object, a list of them, or a `@graph`, and nest
    reservations inside `subjectOf` or `mainEntity`. Walking is simpler than
    enumerating the shapes, and does not go stale when a new one appears.
    """
    if isinstance(payload, list):
        for item in payload:
            yield from _walk(item)
    elif isinstance(payload, dict):
        yield payload
        for value in payload.values():
            if isinstance(value, (list, dict)):
                yield from _walk(value)


# --- the same standard, spelled as HTML attributes ----------------------

#: Elements that never have a closing tag, so they never open a scope and
#: must not be counted when tracking how deep the parser is.
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

#: Where a property's value lives when it is not the element's own text.
_VALUE_ATTRIBUTES = {
    "meta": "content",
    "a": "href",
    "link": "href",
    "img": "src",
    "time": "datetime",
}


class _Microdata(HTMLParser):
    """Collect `itemscope` objects and their `itemprop` values.

    Depth is tracked by counting tags rather than by matching names, because
    an `itemscope` div closes several divs later and matching on the name
    would close it at the first one. Publishers also ship unclosed tags — one
    page in the corpus has an unterminated `<base>` and says so in a comment —
    so the parser has to keep going through markup that does not balance.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._scopes: list[tuple[int, dict]] = []
        self._depth = 0
        self._pending: tuple[dict, str, list[str], int] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {name.lower(): (value or "") for name, value in attrs}
        void = tag.lower() in _VOID
        if not void:
            self._depth += 1

        if "itemscope" in attributes:
            item: dict = {}
            itemtype = attributes.get("itemtype", "").rstrip("/")
            if itemtype:
                item["@type"] = itemtype.rsplit("/", 1)[-1]
            self._attach(attributes.get("itemprop"), item)
            if not void:
                self._scopes.append((self._depth, item))
            return

        prop = attributes.get("itemprop")
        if not prop or not self._scopes:
            return

        attribute = _VALUE_ATTRIBUTES.get(tag.lower())
        value = attributes.get(attribute) if attribute else None
        if value:
            self._attach(prop, value)
        elif not void:
            # The value is whatever the element says on the page.
            self._pending = (self._scopes[-1][1], prop, [], self._depth)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._pending:
            self._pending[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _VOID:
            return
        if self._pending and self._pending[3] == self._depth:
            item, prop, chunks, _ = self._pending
            text = " ".join("".join(chunks).split())
            if text:
                item.setdefault(prop, text)
            self._pending = None
        while self._scopes and self._scopes[-1][0] >= self._depth:
            self._scopes.pop()
        self._depth = max(0, self._depth - 1)

    def _attach(self, prop: str | None, value) -> None:
        if self._scopes:
            parent = self._scopes[-1][1]
            if prop:
                # First writing wins. A property stated twice is a page
                # repeating itself, not a journey happening twice.
                parent.setdefault(prop, value)
            elif isinstance(value, dict):
                self.items.append(value)
        elif isinstance(value, dict):
            self.items.append(value)


def _microdata_reservations(text: str) -> list[dict]:
    if not _HAS_MICRODATA.search(text):
        return []
    parser = _Microdata()
    try:
        parser.feed(text)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not lose the page
        pass

    found = []
    for entry in _walk(parser.items):
        if _RESERVATIONS.get(str(entry.get("@type", "")).casefold()):
            found.append(entry)
    return found


# --- reading one reservation --------------------------------------------


def _record(entry: dict, source_file: str) -> Record | None:
    kind = _RESERVATIONS.get(str(entry.get("@type", "")).casefold())
    booking = entry.get("reservationFor")
    if not isinstance(booking, dict):
        return None

    confirmation = _text(entry.get("reservationNumber"))
    common = dict(
        confirmation=confirmation,
        # Written by the operator's own system, in the vocabulary this
        # application already speaks. There is nothing here to interpret.
        extraction_confidence=0.95,
        provenance=Provenance(extractor="structured", source_file=source_file),
    )

    if kind == "lodging":
        # schema.org renamed these; publishers use both spellings, and a hotel
        # booking is not worth losing over which edition of the vocabulary the
        # site was written against.
        check_in = _time(entry.get("checkinTime") or entry.get("checkinDate"))
        check_out = _time(entry.get("checkoutTime") or entry.get("checkoutDate"))
        if check_in is None:
            return None
        return LodgingRecord(
            property_name=_text(booking.get("name")),
            location=_place(booking),
            check_in=check_in,
            check_out=check_out or check_in,
            **common,
        )

    departure = _time(booking.get("departureTime"))
    if departure is None:
        # The markup may be certain about the flight and unreadable about
        # when: one airline publishes "02-02-2018 18:35", which is not ISO
        # 8601 and does not say whether "02-04" is April or February. These
        # records outrank everything read off the page, so a guessed date
        # would overwrite a correct reading with a confident wrong one.
        # Saying nothing leaves the document to the model, which has the
        # surrounding page to date it by.
        return None
    arrival = _time(booking.get("arrivalTime"))

    if kind == "flight":
        airline = booking.get("airline")
        airline = airline if isinstance(airline, dict) else {}
        return FlightRecord(
            carrier=_text(airline.get("iataCode")),
            number=_text(booking.get("flightNumber")),
            origin=_place(booking.get("departureAirport")),
            destination=_place(booking.get("arrivalAirport")),
            departure=departure,
            arrival=arrival,
            **common,
        )

    provider = booking.get("provider")
    provider = provider if isinstance(provider, dict) else {}
    return TrainRecord(
        mode="bus" if kind == "bus" else "train",
        operator=_text(provider.get("iataCode")) or _text(provider.get("name")),
        number=_text(booking.get("trainNumber") or booking.get("busNumber")),
        origin=_place(booking.get("departureStation") or booking.get("departureBusStop")),
        destination=_place(booking.get("arrivalStation") or booking.get("arrivalBusStop")),
        departure=departure,
        arrival=arrival,
        **common,
    )


def _text(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _place(value) -> Place:
    if not isinstance(value, dict):
        return Place(name=_text(value))
    code = _text(value.get("iataCode"))
    return Place(
        name=_text(value.get("name")),
        iata=code.upper() if code and len(code) == 3 else None,
        detail=code if code and len(code) != 3 else None,
    )


def _time(value) -> LocalTime | None:
    """An ISO 8601 stamp as the wall clock it names.

    The offset says what the local time *was*, not what zone the place keeps,
    so it is used to read the stamp and then dropped: an itinerary shows the
    traveller the time on the departure board, and the zone is resolved later
    from the place itself.
    """
    text = _text(value)
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    # "+0:00" and "+02" are both shipped and neither is ISO 8601. The offset
    # is discarded a line later in any case, but it has to parse first.
    text = re.sub(r"([+-])(\d):(\d{2})$", r"\g<1>0\g<2>:\g<3>", text)
    text = re.sub(r"([+-])(\d{2})$", r"\g<1>\g<2>:00", text)
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return LocalTime(local=stamp.replace(tzinfo=None))
