"""Two lookup tables: who flies a code, and where a station is.

Both answer questions the tool was previously guessing at, and both answer
them the same way every time.

**Airlines.** A ticket prints "S4 246" and a calendar entry saying "S4" tells
the reader nothing. The carrier's name is a lookup, not a judgement, and
asking a model for it spends a call on a fact and gets an invention when the
model does not know. OpenTravelData publishes ~1200 carriers with their IATA
codes, under CC-BY.

**Stations.** A rail ticket carries no IATA code and the station's name is
whatever the operator felt like printing — "MONTPELLIER ST-RO" for Montpellier
Saint-Roch. Without a table, matching that to anything means fuzzy string
comparison, and the timezone has to come from a city the model was asked to
infer. Trainline publish ~71,000 European stations with UIC codes,
coordinates and, decisively, a timezone. Under ODbL: modifications to the data
must be published, which is why nothing here modifies it.

Neither file is committed. They are downloaded on demand and cached in the
data directory, like the airport database, so this repository stays a
repository rather than a redistribution of somebody else's dataset.

Every lookup here is *optional*. A missing file means a field goes unfilled,
never that a document fails to read.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import get_config

#: OpenTravelData's best-known airline list. CC-BY.
AIRLINES_URL = (
    "https://raw.githubusercontent.com/opentraveldata/opentraveldata/master/"
    "opentraveldata/optd_airline_best_known_so_far.csv"
)
#: Trainline's European station list. ODbL.
STATIONS_URL = "https://raw.githubusercontent.com/trainline-eu/stations/master/stations.csv"


@dataclass(frozen=True)
class Station:
    name: str
    uic: str | None
    country: str | None
    timezone: str | None
    latitude: float | None
    longitude: float | None


def _norm(value: str | None) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _tokens(value: str | None) -> list[str]:
    text = "".join(ch if ch.isalnum() else " " for ch in str(value or "").casefold())
    return [word for word in text.split() if word]


# --- airlines -----------------------------------------------------------


@lru_cache(maxsize=1)
def _airlines() -> dict[str, str]:
    """IATA code to airline name.

    A code is not unique: OpenTravelData lists Lufthansa and Lufthansa Cargo
    both under LH, alongside carriers that no longer fly. Looking up "LH"
    naively returns the freight arm, so entries that have ended are skipped and
    the earliest surviving one wins — the incumbent rather than whichever line
    happens to sort first.
    """
    path = get_config().data_dir / "airlines.csv"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    best: dict[str, tuple[str, str]] = {}
    for row in csv.reader(text.splitlines(), delimiter="^"):
        if len(row) < 8 or row[0] == "pk":
            continue
        valid_to, code, name = row[3].strip(), row[5].strip().upper(), row[7].strip()
        if len(code) != 2 or not name or valid_to:
            continue  # No code, no name, or the carrier has stopped flying.
        since = row[2].strip()
        if code not in best or since < best[code][0]:
            best[code] = (since, name)
    return {code: name for code, (_, name) in best.items()}


def airline(code: str | None) -> str | None:
    """The airline that flies under this IATA code, if the table knows it."""
    if not code or len(code.strip()) != 2:
        return None
    return _airlines().get(code.strip().upper())


# --- stations -----------------------------------------------------------


@lru_cache(maxsize=1)
def _stations() -> list[Station]:
    path = get_config().data_dir / "stations.csv"
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return []

    out: list[Station] = []
    with handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue

            def number(key: str) -> float | None:
                try:
                    return float(row.get(key) or "")
                except (TypeError, ValueError):
                    return None

            out.append(
                Station(
                    name=name,
                    uic=(row.get("uic") or "").strip() or None,
                    country=(row.get("country") or "").strip() or None,
                    timezone=(row.get("time_zone") or "").strip() or None,
                    latitude=number("latitude"),
                    longitude=number("longitude"),
                )
            )
    return out


@lru_cache(maxsize=1)
def _by_first() -> dict[str, list[Station]]:
    """Stations bucketed by the first three letters of their first word.

    71,000 names compared word by word is a fifth of a second per lookup, and
    a document has several. The first word has to match the first word for any
    candidate to survive, so bucketing on its opening letters costs nothing and
    skips almost everything. Three letters, not the whole word, because that is
    what survives the contraction rule ("St" for "Saint").
    """
    index: dict[str, list[Station]] = {}
    for station_row in _stations():
        words = _tokens(station_row.name)
        if words:
            index.setdefault(words[0][:3], []).append(station_row)
    return index


@lru_cache(maxsize=1)
def _by_name() -> dict[str, Station]:
    index: dict[str, Station] = {}
    for station in _stations():
        index.setdefault(_norm(station.name), station)
    return index


def station(name: str | None) -> Station | None:
    """The station an operator means, allowing for how it abbreviates.

    Exact first, then word by word: every operator shortens its own station
    names and differently, and SNCF's "MONTPELLIER ST-RO" is Montpellier
    Saint-Roch. Each word of the printed name has to begin — or contract to —
    the matching word of the candidate, and a single candidate has to survive.
    Two Paris termini must never resolve to each other, so an ambiguous match
    is no match: a wrong station is worse than an unresolved one.
    """
    if not name or not _stations():
        return None

    exact = _by_name().get(_norm(name))
    if exact is not None:
        return exact

    wanted = _tokens(name)
    if len(wanted) < 2:
        return None  # One word is not enough to pick a station out of 71,000.

    matches = [s for s in _by_first().get(wanted[0][:3], []) if _same_name(wanted, _tokens(s.name))]
    if not matches:
        return None

    # The shortest name, when the others are all extensions of it: a ticket
    # saying "Paris Nord" means Paris Gare du Nord, not "Paris Gare du Nord 2"
    # or the Eurostar hall inside it. Where the alternatives are genuinely
    # different stations, none of them is a prefix of the rest and the lookup
    # gives up — a wrong station is worse than an unresolved one.
    matches.sort(key=lambda s: len(_tokens(s.name)))
    shortest = _tokens(matches[0].name)
    if all(_tokens(s.name)[: len(shortest)] == shortest for s in matches):
        return matches[0]
    return None


def _same_name(wanted: list[str], candidate: list[str]) -> bool:
    """Does every word of the printed name appear, in order, in the candidate?

    In order and not necessarily adjacent, because operators drop the words
    that carry no information: "Paris Nord" is Paris Gare du Nord, and
    demanding the same number of words would never match it. The first word
    must still be the first, so "Nord" alone matches nothing, and the whole
    result has to be unique — an ambiguous match is treated as no match,
    because a wrong station is worse than an unresolved one.
    """
    if len(wanted) > len(candidate) or not wanted:
        return False
    if not _same_word(wanted[0], candidate[0]):
        return False

    remaining = iter(candidate[1:])
    return all(
        any(_same_word(word, other) for other in remaining) for word in wanted[1:]
    )


def _same_word(printed: str, full: str) -> bool:
    if full.startswith(printed) or printed.startswith(full):
        return True
    # "St" for "Saint" is a contraction, not a prefix: the letters are kept in
    # order but the middle is dropped. Either side may be the short one — a
    # ticket abbreviates, a timetable spells out — and only short words
    # qualify, or any two sharing a first letter would start matching.
    short, long = (printed, full) if len(printed) <= len(full) else (full, printed)
    if len(short) <= 3 and long[:1] == short[:1]:
        remaining = iter(long)
        return all(letter in remaining for letter in short)
    return False


def clear_cache() -> None:
    """Forget the loaded tables, so a fresh download is seen."""
    for cached in (_airlines, _stations, _by_name, _by_first):
        cached.cache_clear()


def available() -> tuple[bool, bool]:
    """(airlines, stations) — which tables are present."""
    return bool(_airlines()), bool(_stations())
