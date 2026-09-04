"""Did we get everything the document lists?

Every other validator asks whether a record is right. This one asks whether a
record is *missing*, which is the failure the rest of the pipeline cannot see:
a leg that was never extracted has no fields to check, no timezone to resolve
and no distance to fail, so it sails through every test and the itinerary looks
perfect. One real receipt listed two flights, and the single record extracted
from it was promoted to a live calendar at 90% confidence.

The check is deliberately blunt and deterministic. A service designator —
"S4246", "BA 117", "AF3611" — is machine-written and appears in the text
whatever the layout does. If the document names one that no record claims,
something was dropped, and nothing from that document should reach a calendar
unreviewed.
"""

from __future__ import annotations

import re

from ..schema import FlightRecord, IssueLevel, Itinerary, TrainRecord

SOURCE = "completeness"

#: An IATA-style designator: a two-character carrier code (letters, or a letter
#: and a digit) then one to four digits. Anchored on a word boundary and
#: required to be followed by one, so dates, prices and phone numbers do not
#: match.
_DESIGNATOR_RE = re.compile(r"\b([A-Z][A-Z0-9]|[0-9][A-Z])\s?(\d{1,4})\b")

#: Strings that look exactly like a designator but never are. Ticket numbers,
#: terminals and class codes sit next to real ones on the same page.
_NOT_A_SERVICE = re.compile(
    r"(terminal|class|classe|gate|seat|pc|kg|lbs?|eur|usd|tel|fax)\W{0,3}$", re.I
)


def _designators_in(text: str) -> set[str]:
    """Every service number the document itself prints."""
    found: set[str] = set()
    for match in _DESIGNATOR_RE.finditer(text):
        # A designator immediately preceded by a label like "Terminal" is that
        # label's value, not a flight.
        if _NOT_A_SERVICE.search(text[max(0, match.start() - 14) : match.start()]):
            continue
        found.add(f"{match.group(1)}{int(match.group(2))}")
    return found


def _designators_claimed(itinerary: Itinerary) -> set[str]:
    claimed: set[str] = set()
    for record in itinerary.records:
        if not isinstance(record, (FlightRecord, TrainRecord)):
            continue
        operator = getattr(record, "carrier", None) or getattr(record, "operator", None)
        number = getattr(record, "number", None)
        if operator and number and str(number).isdigit():
            claimed.add(f"{operator.upper()[:2]}{int(number)}")
    return claimed


def missing_designators(text: str, records: list) -> list[str]:
    """Services the document names that no record claims.

    Split out from ``run`` because the pipeline uses it to go back and ask for
    the missing leg specifically, which is worth far more than a warning.
    """
    trip = Itinerary()
    trip.records = list(records)
    claimed = _designators_claimed(trip)
    if not claimed:
        # Nothing to compare against: a lodging booking, or a document whose
        # legs carry no numbers. Counting designators here would be noise.
        return []

    # Only designators sharing a carrier with something we did extract. Any
    # other two-letter-plus-digits string on the page is a fare code, a phone
    # extension or a form number, and guessing otherwise would hold every
    # clean submission.
    carriers = {code[:2] for code in claimed}
    return sorted(code for code in _designators_in(text) - claimed if code[:2] in carriers)


def run(itinerary: Itinerary) -> Itinerary:
    if not itinerary.source_text:
        return itinerary

    for name, text in itinerary.source_text.items():
        missing = missing_designators(text, itinerary.records)
        if not missing:
            continue

        itinerary.add_issue(
            IssueLevel.WARN,
            "itinerary.leg_possibly_missing",
            f"'{name}' also mentions {', '.join(missing)}, which no extracted record "
            "claims. A leg of this journey may be missing — check the document before "
            "trusting what was added.",
            SOURCE,
        )
        # On every record, not just the itinerary: an itinerary-level issue
        # does not hold anything back, and the whole point is that a document
        # with a dropped leg must not be promoted unreviewed.
        for record in itinerary.records:
            record.add_issue(
                IssueLevel.WARN,
                "itinerary.leg_possibly_missing",
                f"The document also mentions {', '.join(missing)}, which was not "
                "extracted. Check whether this booking has another leg.",
                SOURCE,
            )
    return itinerary
