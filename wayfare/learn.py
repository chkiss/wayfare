"""Learn a user's calendar conventions from their own past events.

The tool has to choose what an event is called, and any default it picks will
be wrong for somebody. Rather than guess, `wayfare learn` reads an exported
calendar, finds the travel events already in it, and works out the conventions
they follow: the separator between airports, whether the flight number leads
the title, whether hotels are one event or two, what ends up in the
description.

The output is a conventions file and a report of what it saw and how
consistent it was, so a weak signal reads as a weak signal rather than as a
confident recommendation.

Nothing here leaves the machine, and the report names patterns, not
itineraries.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path

from .icsparse import VEvent

# --- classification ------------------------------------------------------

#: Two airport-ish tokens with something between them: "LHR → JFK", "LHR-JFK".
#: Case-sensitive on purpose. Matching case-insensitively turns the perfectly
#: ordinary name "Mae-Mae Hazen" into a route, and it did.
ROUTE_RE = re.compile(r"\b([A-Z]{3})\b\s*(→|->|—|–|-|to|/|>)\s*\b([A-Z]{3})\b")
#: "BA117", "BA 117", "AC 834".
DESIGNATOR_RE = re.compile(r"\b([A-Z]{2}|[A-Z]\d|\d[A-Z])\s?(\d{1,4})\b")

FLIGHT_WORDS = re.compile(
    r"\b(flight|flug|vuelo|vol|airways|airlines|airline|boarding|depart|dep\b|arr\b)\b", re.I
)
#: Words that genuinely mean lodging. Deliberately narrow. "check-in",
#: "checkout", "night" and "stay" were all in this list and all of them were
#: wrong: they matched "Carlucci Fellowship Check-in", "Lab Checkout", "One
#: Night of Queen" and "Doha check-in" (which is a flight).
LODGING_WORDS = re.compile(
    r"\b(hotel|hostel|airbnb|motel|inn|resort|lodge|guest\s?house|b&b|riad|"
    r"stay\s+at|staying\s+at)\b",
    re.I,
)
RAIL_WORDS = re.compile(
    r"\b(train|rail|eurostar|amtrak|thalys|sncf|ice\b|tgv|via rail|station|platform)\b", re.I
)
CHECKIN_RE = re.compile(r"check[\s-]?in", re.I)
CHECKOUT_RE = re.compile(r"check[\s-]?out", re.I)

CONFIRMATION_WORDS = re.compile(
    r"\b(confirmation|conf\b|booking\s?(ref|reference|number)?|pnr|record locator|reservation)\b",
    re.I,
)
SEAT_RE = re.compile(r"\bseat\b", re.I)
TERMINAL_RE = re.compile(r"\bterminal\b", re.I)

#: Leading emoji or symbol used as a category marker.
PREFIX_RE = re.compile(r"^([^\w\s]{1,2})\s*")


def _real_route(summary: str):
    """A route match whose codes are both real airports.

    Without this check, "Sanford MPP/MBA Call" is a flight from MPP to MBA.
    The airport database is already loaded for the validators, so confirming a
    code costs nothing.
    """
    from .airports import get_airport_db

    db = get_airport_db()
    for match in ROUTE_RE.finditer(summary):
        if not db.available:
            return match  # No database: fall back to the shape alone.
        if db.get(match.group(1)) and db.get(match.group(3)):
            return match
    return None


@lru_cache(maxsize=1)
def _city_names() -> set[str]:
    """Every municipality that has an airport, for spotting city names."""
    from .airports import get_airport_db

    db = get_airport_db()
    if not db.available:
        return set()
    names = set()
    for airport in db._load().values():  # noqa: SLF001 - same package
        city = airport.municipality.strip()
        if len(city) >= 3:
            names.add(city.casefold())
    return names


def _find_city(summary: str) -> str | None:
    """The longest run of capitalised words that names a city with an airport."""
    words = re.findall(r"[A-Z][\w'’-]+", summary)
    for length in (3, 2, 1):
        for start in range(len(words) - length + 1):
            candidate = " ".join(words[start : start + length])
            if candidate.casefold() in _city_names():
                return candidate
    return None


def title_template(summary: str) -> str | None:
    """Reduce a real title to the shape it follows.

    "Flight to Paris (AF 3611)" becomes
    "Flight to {destination_city} ({carrier} {number})", so 209 individual
    titles collapse into a handful of templates that can be counted. This is
    what makes the conventions come from the calendar rather than from taste.
    """
    template = summary.strip()
    if not template:
        return None

    designator = DESIGNATOR_RE.search(template)
    if designator:
        token = "{carrier} {number}" if " " in designator.group(0) else "{carrier_number}"
        template = template.replace(designator.group(0), token, 1)

    route = _real_route(template)
    if route:
        template = template.replace(
            route.group(0), "{origin} " + route.group(2).strip() + " {destination}", 1
        )
    else:
        city = _find_city(template)
        if city:
            template = re.sub(rf"\b{re.escape(city)}\b", "{destination_city}", template, count=1)

    return " ".join(template.split())


@dataclass
class Findings:
    total_events: int = 0
    flights: list[VEvent] = field(default_factory=list)
    lodging: list[VEvent] = field(default_factory=list)
    rail: list[VEvent] = field(default_factory=list)
    counters: dict[str, Counter] = field(default_factory=lambda: {})

    def counter(self, name: str) -> Counter:
        return self.counters.setdefault(name, Counter())


def classify(event: VEvent) -> str | None:
    """Which kind of travel event this is, if any.

    Order matters. Rail is tested first because "Eurostar 9145 BRU - LON" looks
    exactly like a flight to the route pattern. Lodging is tested last and by
    shape as well as by wording, because most hotels are not called "hotel" —
    Hyatt Regency, Ibis, The Standard — and a keyword list would miss them.
    """
    summary = event.summary

    if RAIL_WORDS.search(summary):
        return "rail"

    if _real_route(summary) or (DESIGNATOR_RE.search(summary) and FLIGHT_WORDS.search(summary)):
        return "flight"
    if FLIGHT_WORDS.search(summary):
        return "flight"
    # "New York to Riyadh – SV 22": city names rather than codes, and no word
    # anywhere that says "flight". The flight number plus a "to" is what
    # identifies it.
    if DESIGNATOR_RE.search(summary) and _find_city(summary) and re.search(r"\bto\b", summary, re.I):
        return "flight"

    # Keywords are read from the title only. A hotel in the *location* means
    # the event happens at a hotel, not that it is a hotel booking: it matched
    # "drinks", "COVID-19 test" and a wedding, all held in hotels.
    if LODGING_WORDS.search(summary):
        return "lodging"
    if _looks_like_a_stay(event):
        return "lodging"
    return None


def _looks_like_a_stay(event: VEvent) -> bool:
    """A timed multi-night event with a place attached, in check-in hours.

    This catches the hotel whose name gives nothing away — Hyatt Regency, Ibis,
    The Standard.

    All-day events are excluded entirely. On a real calendar they are trips and
    visits, not bookings: "New York w Joe & Nesli", "UAE with Joe", "Halah
    Habib wedding" all matched when they were allowed in, and every one of them
    was wrong. Requiring a timed event with an afternoon start and a late
    morning end is what separates a hotel from a holiday.
    """
    days = event.duration_days
    if days is None or not 1 <= days <= 30:
        return False
    if event.all_day or not event.location.strip():
        return False

    start, end = event.start, event.end
    if not hasattr(start, "hour") or not hasattr(end, "hour"):
        return False
    return 11 <= start.hour <= 22 and 5 <= end.hour <= 13


def collect(events: list[VEvent]) -> Findings:
    findings = Findings(total_events=len(events))
    for event in events:
        if not event.summary.strip():
            continue
        kind = classify(event)
        if kind == "flight":
            findings.flights.append(event)
        elif kind == "lodging":
            findings.lodging.append(event)
        elif kind == "rail":
            findings.rail.append(event)
    return findings


# --- pattern extraction --------------------------------------------------


def _prefix(summary: str) -> str | None:
    match = PREFIX_RE.match(summary)
    return match.group(1) if match else None


def _route_separator(summary: str) -> str | None:
    match = ROUTE_RE.search(summary)
    if not match:
        return None
    separator = match.group(2).strip()
    return separator if separator else None


def _designator_style(summary: str) -> str | None:
    """Is the flight number written 'BA117' or 'BA 117', and does it lead?"""
    match = DESIGNATOR_RE.search(summary)
    if not match:
        return None
    spaced = " " in match.group(0)
    leading = summary.strip().lstrip("".join(_prefix(summary) or "")).strip().startswith(
        match.group(0)
    )
    return f"{'spaced' if spaced else 'joined'}-{'leading' if leading else 'trailing'}"


def analyse(findings: Findings) -> dict:
    """Turn the collected events into counted patterns."""
    for event in findings.flights:
        summary = event.summary
        template = title_template(summary)
        if template:
            findings.counter("flight_template")[template] += 1
        findings.counter("flight_prefix")[_prefix(summary) or ""] += 1
        separator = _route_separator(summary)
        if separator:
            findings.counter("route_separator")[separator] += 1
        style = _designator_style(summary)
        if style:
            findings.counter("designator_style")[style] += 1
        findings.counter("flight_has_number")[bool(DESIGNATOR_RE.search(summary))] += 1
        findings.counter("flight_has_route")[bool(ROUTE_RE.search(summary))] += 1
        findings.counter("flight_uses_codes")[
            bool(ROUTE_RE.search(summary)) and summary.upper() == summary or _uses_codes(summary)
        ] += 1
        if event.description:
            findings.counter("flight_desc_confirmation")[
                bool(CONFIRMATION_WORDS.search(event.description))
            ] += 1
            findings.counter("flight_desc_seat")[bool(SEAT_RE.search(event.description))] += 1
            findings.counter("flight_desc_terminal")[
                bool(TERMINAL_RE.search(event.description))
            ] += 1
        findings.counter("flight_has_location")[bool(event.location.strip())] += 1
        findings.counter("flight_all_day")[event.all_day] += 1

    for event in findings.lodging:
        summary = event.summary
        findings.counter("lodging_template")[title_template(summary) or ""] += 1
        findings.counter("lodging_prefix")[_prefix(summary) or ""] += 1
        if CHECKIN_RE.search(summary):
            findings.counter("lodging_shape")["checkin_event"] += 1
        elif CHECKOUT_RE.search(summary):
            findings.counter("lodging_shape")["checkout_event"] += 1
        else:
            days = event.duration_days
            if days is not None and days >= 1:
                findings.counter("lodging_shape")["span"] += 1
            else:
                findings.counter("lodging_shape")["point"] += 1
        findings.counter("lodging_all_day")[event.all_day] += 1
        if event.description:
            findings.counter("lodging_desc_confirmation")[
                bool(CONFIRMATION_WORDS.search(event.description))
            ] += 1
        findings.counter("lodging_has_location")[bool(event.location.strip())] += 1

    for event in findings.rail:
        findings.counter("rail_prefix")[_prefix(event.summary) or ""] += 1
        separator = _route_separator(event.summary)
        if separator:
            findings.counter("rail_separator")[separator] += 1

    return findings.counters


def _uses_codes(summary: str) -> bool:
    """Does the route use IATA codes rather than city names?"""
    match = ROUTE_RE.search(summary)
    if not match:
        return False
    return match.group(1).isupper() and match.group(3).isupper()


def _dominant(counter: Counter, minimum: int = 3) -> tuple[object, float, int]:
    """The most common value, its share, and how many events it was seen in."""
    if not counter:
        return None, 0.0, 0
    total = sum(counter.values())
    value, count = counter.most_common(1)[0]
    if total < minimum:
        return None, 0.0, total
    return value, count / total, total


# --- output --------------------------------------------------------------


def propose_conventions(counters: dict[str, Counter]) -> tuple[dict, list[str]]:
    """Build a conventions dict, plus a note for every choice that was weak."""
    conventions: dict[str, object] = {}
    notes: list[str] = []

    # The whole title, taken as one shape, beats assembling it from separately
    # counted parts. Patching a template piece by piece produced
    # "{carrier} {number} {origin} to {destination} ({carrier_number})" — a
    # title that named the flight twice and matched nothing in the calendar.
    template, share, seen = _dominant(counters.get("flight_template", Counter()))
    if isinstance(template, str) and template:
        conventions["flight_title"] = template
        if share < 0.5:
            notes.append(
                f"The most common flight title shape covered only {share:.0%} of "
                f"{seen} flights, so this is the plurality rather than a habit."
            )
    else:
        notes.append("No repeated flight title shape found; keeping the default.")

    prefix, share, seen = _dominant(counters.get("flight_prefix", Counter()))
    if prefix:
        conventions["title_prefix"] = f"{prefix} "
        if share < 0.7:
            notes.append(f"Prefix '{prefix}' used in {share:.0%} of flight titles only.")

    # Lodging needs a higher bar than the rest. Plenty of people never put
    # hotels in their calendar at all, and reading a convention out of three
    # events would be inventing one.
    shape, share, seen = _dominant(counters.get("lodging_shape", Counter()), minimum=8)
    if shape in ("checkin_event", "checkout_event"):
        conventions["lodging_style"] = "endpoints"
    elif shape == "span":
        conventions["lodging_style"] = "span"

    if shape is None:
        notes.append(
            f"Only {seen} hotel events found, too few to read a convention from. "
            "Lodging style left at the default; set it yourself."
        )
    elif share < 0.7:
        notes.append(
            f"Hotels were recorded as '{shape}' in only {share:.0%} of {seen} stays; "
            "check this one."
        )

    lodging_template, share, seen = _dominant(counters.get("lodging_template", Counter()))
    if isinstance(lodging_template, str) and lodging_template and share >= 0.5:
        conventions["lodging_title"] = lodging_template

    lodging_prefix, share, _ = _dominant(counters.get("lodging_prefix", Counter()))
    if lodging_prefix and lodging_prefix != conventions.get("title_prefix", "").strip():
        conventions["lodging_title"] = f"{lodging_prefix} {{property_name}}"

    confirmation, share, seen = _dominant(counters.get("flight_desc_confirmation", Counter()))
    if confirmation is not None:
        conventions["include_confirmation"] = bool(confirmation)
        if share < 0.7:
            notes.append(
                f"Confirmation numbers appeared in {share:.0%} of flight descriptions."
            )

    return conventions, notes


def report(findings: Findings, counters: dict[str, Counter], conventions: dict, notes: list[str]) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"Read {findings.total_events} events.")
    add(
        f"  flights {len(findings.flights)} · lodging {len(findings.lodging)} "
        f"· rail {len(findings.rail)}"
    )
    add("")

    if len(findings.flights) + len(findings.lodging) < 20:
        add("Warning: fewer than 20 travel events found. The conventions below are a guess,")
        add("not a pattern. Export a longer date range if you can.")
        add("")

    interesting = [
        ("Route separator", "route_separator"),
        ("Flight number style", "designator_style"),
        ("Flight title prefix", "flight_prefix"),
        ("Flight title has number", "flight_has_number"),
        ("Hotel event shape", "lodging_shape"),
        ("Hotel title prefix", "lodging_prefix"),
        ("Confirmation in description", "flight_desc_confirmation"),
        ("Seat in description", "flight_desc_seat"),
        ("Terminal in description", "flight_desc_terminal"),
        ("Location field used", "flight_has_location"),
    ]
    add("What your calendar does:")
    for label, key in interesting:
        counter = counters.get(key)
        if not counter:
            continue
        total = sum(counter.values())
        parts = ", ".join(
            f"{value if value != '' else '(none)'} {count / total:.0%}"
            for value, count in counter.most_common(4)
        )
        add(f"  {label:<28} {parts}   (n={total})")

    templates = counters.get("flight_template")
    if templates:
        total = sum(templates.values())
        add("")
        add("Flight title shapes, most common first:")
        for shape, count in templates.most_common(5):
            add(f"  {count / total:>4.0%}  {shape}")

    add("")
    add("Proposed conventions:")
    for key, value in sorted(conventions.items()):
        add(f"  {key}: {value!r}")

    if notes:
        add("")
        add("Where the signal was weak:")
        for note in notes:
            add(f"  - {note}")

    return "\n".join(lines)


def learn(events: list[VEvent]) -> tuple[dict, str]:
    findings = collect(events)
    counters = analyse(findings)
    conventions, notes = propose_conventions(counters)
    return conventions, report(findings, counters, conventions, notes)


def write_conventions(conventions: dict, path: Path) -> Path:
    from .render import DEFAULT_CONVENTIONS

    merged = dict(DEFAULT_CONVENTIONS)
    merged.update(conventions)
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
