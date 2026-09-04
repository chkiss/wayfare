"""Scoring wayfare against a corpus of real documents with known answers.

Every measurement in this project so far has come from running one document
and reading the output. That found real bugs — a leg silently dropped, a
flight number the page contradicted — but it cannot answer the question that
matters before a change ships: is this better or worse than what we had?

KItinerary's test corpus answers it. A few hundred real booking documents,
each paired with the reservation it should produce, in schema.org vocabulary
that maps almost directly onto wayfare's own records. It is the only labelled
travel corpus that is public, because a real boarding pass carries a working
booking reference and nobody can publish those.

Two things this deliberately does not do:

* **Guess at a passing grade.** It reports what matched and what did not, per
  field and per category. A number is only useful next to the previous number.
* **Reward finding nothing.** A document that produces no records at all
  scores zero for every field it should have filled, rather than being
  quietly absent from the denominator. Missing legs is the failure this whole
  pipeline is built against, and a scoreboard that ignores them would point
  the wrong way.

Get the corpus with:

    git clone --depth 1 https://invent.kde.org/pim/kitinerary.git
    wayfare bench kitinerary/autotests/extractordata
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .schema import FlightRecord, Itinerary, LodgingRecord, Record, TrainRecord

#: What we can actually put through `ingest`. The corpus also carries raw
#: UIC 918.3 rail barcodes (.bin), Apple Wallet passes and HAR captures, which
#: wayfare has no reader for — counted as skipped rather than as failures,
#: because scoring a format the tool never claimed to read tells you nothing.
READABLE = {".txt", ".eml", ".ics", ".html", ".htm", ".pdf", ".json", ".md"}

#: schema.org reservation types, mapped to what wayfare calls them.
KINDS = {
    "FlightReservation": "flight",
    "TrainReservation": "train",
    "BusReservation": "bus",
    "LodgingReservation": "lodging",
}


@dataclass
class Expected:
    """One reservation the document is known to contain."""

    kind: str
    carrier: str | None = None
    number: str | None = None
    origin: str | None = None
    destination: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    confirmation: str | None = None


@dataclass
class Case:
    """A document and the reservations it should produce."""

    path: Path
    name: str
    expected: list[Expected]

    @property
    def category(self) -> str:
        kinds = {e.kind for e in self.expected}
        return kinds.pop() if len(kinds) == 1 else "mixed"


@dataclass
class Result:
    case: Case
    found: int = 0
    fields: dict[str, tuple[int, int]] = field(default_factory=dict)
    error: str | None = None

    @property
    def expected_count(self) -> int:
        return len(self.case.expected)

    @property
    def count_ok(self) -> bool:
        return self.found == self.expected_count


# --- reading the corpus -------------------------------------------------


def _stamp(value) -> datetime | None:
    """A schema.org date-time, as a naive local datetime.

    Naive on purpose: the corpus states local time with an offset, wayfare
    stores local time with a zone name, and comparing the two through UTC
    would turn a timezone bug into a passing test.
    """
    if isinstance(value, dict):
        value = value.get("@value")
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Drop the offset: "2018-07-15T17:50:00+02:00" -> "2018-07-15T17:50:00".
    if len(text) > 19 and (text[19] in "+-" or text.endswith("Z")):
        text = text[:19]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _place(node) -> str | None:
    """An airport code where there is one, otherwise the station's name."""
    if not isinstance(node, dict):
        return None
    return node.get("iataCode") or node.get("name") or None


def _expected_from(entry: dict) -> Expected | None:
    kind = KINDS.get(str(entry.get("@type")))
    if kind is None:
        return None

    booking = entry.get("reservationFor") or {}
    confirmation = entry.get("reservationNumber")

    if kind == "lodging":
        return Expected(
            kind=kind,
            destination=(booking.get("name") if isinstance(booking, dict) else None),
            start=_stamp(entry.get("checkinTime")),
            end=_stamp(entry.get("checkoutTime")),
            confirmation=confirmation,
        )

    airline = booking.get("airline") or {}
    provider = booking.get("provider") or {}
    return Expected(
        kind=kind,
        carrier=(airline.get("iataCode") or provider.get("iataCode") or None),
        number=(
            booking.get("flightNumber")
            or booking.get("trainNumber")
            or booking.get("busNumber")
            or None
        ),
        origin=_place(
            booking.get("departureAirport")
            or booking.get("departureStation")
            or booking.get("departureBusStop")
        ),
        destination=_place(
            booking.get("arrivalAirport")
            or booking.get("arrivalStation")
            or booking.get("arrivalBusStop")
        ),
        start=_stamp(booking.get("departureTime")),
        end=_stamp(booking.get("arrivalTime")),
        confirmation=confirmation,
    )


def load_corpus(root: Path, only: str | None = None) -> list[Case]:
    """Every document under `root` that has an answer beside it."""
    cases: list[Case] = []
    for answer in sorted(root.rglob("*.json")):
        source = answer.with_suffix("")  # "x.txt.json" -> "x.txt"
        if not source.exists() or source.suffix.lower() not in READABLE:
            continue
        try:
            payload = json.loads(answer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, list):
            continue

        expected = [e for e in (_expected_from(x) for x in payload if isinstance(x, dict)) if e]
        if not expected:
            continue

        case = Case(path=source, name=str(source.relative_to(root)), expected=expected)
        if only and case.category != only:
            continue
        cases.append(case)
    return cases


# --- comparing ----------------------------------------------------------


def _norm(value) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _tokens(value) -> list[str]:
    text = "".join(ch if ch.isalnum() else " " for ch in str(value or "").casefold())
    return [word for word in text.split() if word]


def _same_name(want: str, got: str) -> bool:
    """Is this the same station, allowing for the operator's abbreviations?

    Every operator shortens its own station names, and differently: SNCF
    prints "MONTPELLIER ST-RO" for Montpellier Saint-Roch. Comparing whole
    strings marks that wrong, and comparing loosely marks two different Paris
    termini right. Word by word, each word of the shorter name having to begin
    the matching word of the longer, does neither.
    """
    mine, theirs = _tokens(want), _tokens(got)
    if not mine or not theirs:
        return False
    short, long = (mine, theirs) if len(mine) <= len(theirs) else (theirs, mine)
    if len(short) < len(long) and len(short) < 2:
        return False  # One word standing in for three says too little.
    return all(_same_word(word, long[index]) for index, word in enumerate(short))


def _same_word(short: str, long: str) -> bool:
    if long.startswith(short) or short.startswith(long):
        return True
    # "St" for "Saint" is a contraction, not a prefix: the letters are kept in
    # order but the middle is dropped. Allowed only for very short words, or
    # any two words sharing a first letter would start matching.
    if len(short) <= 3 and long[:1] == short[:1]:
        remaining = iter(long)
        return all(letter in remaining for letter in short)
    return False


def _same_place(want: str | None, got_record, side: str) -> bool | None:
    """Did we identify the place? None when the corpus does not say."""
    if not want:
        return None
    place = getattr(got_record, side, None)
    if place is None:
        return False

    wanted = _norm(want)
    for candidate in (place.iata, place.name, place.city):
        if not candidate:
            continue
        if _norm(candidate) == wanted:
            return True
        # An airport code is compared whole; a station name word by word.
        if len(wanted) > 3 and _same_name(want, candidate):
            return True
    return False


def _same_number(want: str | None, record) -> bool | None:
    """Did we identify the service, however the corpus writes it?

    The corpus puts the operator inside the number for rail — `trainNumber` is
    "ICE 16" — while wayfare keeps `operator` and `number` apart. Comparing the
    raw strings marked every correct train wrong, which read as 2% when it was
    nearer all of them.
    """
    if not want:
        return None

    number = str(getattr(record, "number", "") or "")
    operator = str(
        getattr(record, "carrier", None) or getattr(record, "operator", None) or ""
    )
    wanted = _norm(want).lstrip("0")
    return wanted in {
        _norm(number).lstrip("0"),
        _norm(f"{operator}{number}").lstrip("0"),
    }


def _same_time(want: datetime | None, got) -> bool | None:
    if want is None:
        return None
    if got is None or getattr(got, "local", None) is None:
        return False
    return got.local.replace(second=0, microsecond=0) == want.replace(second=0, microsecond=0)


def _kind_of(record: Record) -> str:
    if isinstance(record, FlightRecord):
        return "flight"
    if isinstance(record, TrainRecord):
        return getattr(record, "mode", "train") or "train"
    if isinstance(record, LodgingRecord):
        return "lodging"
    return "other"


def _pair(expected: list[Expected], records: list[Record]) -> list[tuple[Expected, Record | None]]:
    """Line each expected reservation up with the record that best fits it.

    Best fit rather than order: a reading that returns the return leg first is
    not wrong about the return leg, and scoring by position would say it got
    every field of both legs wrong.
    """
    unclaimed = list(records)
    pairs: list[tuple[Expected, Record | None]] = []

    for want in expected:
        best, score = None, 0
        for record in unclaimed:
            points = 0
            if _kind_of(record) == want.kind:
                points += 2
            if _same_number(want.number, record) is True:
                points += 3
            start = getattr(record, "departure", None) or getattr(record, "check_in", None)
            if want.start and start is not None and getattr(start, "local", None) is not None:
                if start.local.date() == want.start.date():
                    points += 2
            if points > score:
                best, score = record, points
        if best is not None:
            unclaimed.remove(best)
        pairs.append((want, best))
    return pairs


def compare(case: Case, itinerary: Itinerary) -> Result:
    """Score one document. Absent records fail every field they should have filled."""
    result = Result(case=case, found=len(itinerary.records))
    tally: dict[str, list[int]] = {}

    def note(name: str, ok: bool | None) -> None:
        if ok is None:
            return  # The corpus does not state this field; nothing to score.
        got, total = tally.setdefault(name, [0, 0])
        tally[name] = [got + (1 if ok else 0), total + 1]

    for want, record in _pair(case.expected, list(itinerary.records)):
        if record is None:
            # Nothing found for this reservation. Every field the corpus
            # states is a field we failed to produce.
            note("kind", False)
            for name, value in (
                ("number", want.number),
                ("origin", want.origin),
                ("destination", want.destination),
                ("start", want.start),
                ("end", want.end),
                ("confirmation", want.confirmation),
            ):
                if value:
                    note(name, False)
            continue

        note("kind", _kind_of(record) == want.kind)
        note("number", _same_number(want.number, record))
        note("origin", _same_place(want.origin, record, "origin"))
        note("destination", _same_place(want.destination, record, "destination")
             if want.kind != "lodging"
             else _same_place(want.destination, record, "location"))
        note("start", _same_time(
            want.start, getattr(record, "departure", None) or getattr(record, "check_in", None)))
        note("end", _same_time(
            want.end, getattr(record, "arrival", None) or getattr(record, "check_out", None)))
        note(
            "confirmation",
            None if not want.confirmation
            else _norm(want.confirmation) == _norm(record.confirmation),
        )

    result.fields = {name: (got, total) for name, (got, total) in tally.items()}
    return result


# --- running ------------------------------------------------------------


def run(
    root: Path,
    limit: int | None = None,
    only: str | None = None,
    use_llm: bool = False,
    progress=None,
) -> list[Result]:
    """Read every case and score it.

    The model is off unless asked for. A hundred documents is several hundred
    model requests, and a free tier allows fifty a day — a benchmark that
    quietly spends the day's budget is worse than no benchmark.
    """
    from . import pipeline

    previous = os.environ.get("WAYFARE_DISABLE_LLM")
    if not use_llm:
        os.environ["WAYFARE_DISABLE_LLM"] = "1"

    try:
        cases = load_corpus(root, only=only)
        if limit:
            cases = cases[:limit]

        results = []
        for index, case in enumerate(cases, start=1):
            if progress:
                progress(index, len(cases), case)
            try:
                itinerary = pipeline.process_file(case.path, case.path.name)
                results.append(compare(case, itinerary))
            except Exception as exc:  # noqa: BLE001 - a crash is a result too
                # Scored as though it had found nothing, which is what it did.
                # Counting the error and stopping there would drop the
                # document's legs out of the denominator, and a crash would
                # score better than a wrong answer — the same trap as
                # rewarding an empty reading, arriving by another road.
                result = compare(case, Itinerary())
                result.error = f"{type(exc).__name__}: {exc}"
                results.append(result)
        return results
    finally:
        if previous is None:
            os.environ.pop("WAYFARE_DISABLE_LLM", None)
        else:
            os.environ["WAYFARE_DISABLE_LLM"] = previous


def summarise(results: list[Result]) -> dict:
    """The numbers worth putting next to last week's numbers."""
    by_category: dict[str, dict] = {}
    fields: dict[str, list[int]] = {}

    for result in results:
        bucket = by_category.setdefault(
            result.case.category,
            {"documents": 0, "right_count": 0, "expected": 0, "found": 0, "errors": 0},
        )
        bucket["documents"] += 1
        bucket["expected"] += result.expected_count
        bucket["found"] += result.found
        if result.error:
            bucket["errors"] += 1
        elif result.count_ok:
            bucket["right_count"] += 1
        for name, (got, total) in result.fields.items():
            slot = fields.setdefault(name, [0, 0])
            slot[0] += got
            slot[1] += total

    return {
        "documents": len(results),
        "errors": sum(1 for r in results if r.error),
        "categories": by_category,
        "fields": {name: tuple(counts) for name, counts in sorted(fields.items())},
    }
