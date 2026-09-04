"""Did we get everything the document lists?

Every other validator asks whether a record is right. This one asks whether a
record is *missing*, which is the failure the rest of the pipeline cannot see:
a leg that was never extracted has no fields to check, no timezone to resolve
and no distance to fail, so it sails through every test and the itinerary looks
perfect. One real receipt listed two flights, and the single record extracted
from it was promoted to a live calendar at 90% confidence.

The counting lives in ``manifest``, which reads the document from several
independent signals. This module compares that against what came out, and is
careful about the comparison in two ways.

A signal only counts against records it could have seen. A route signal cannot
notice a missing flight if the document never wrote a coded route, so it is
compared only with records that have coded routes. Mixing them produced a
warning on every ticket that spelled its airports out in full.

And a named journey is only reported missing if the name is one this document
uses for its journeys. Any page has stray two-letter-plus-digits strings —
fare bases, phone extensions, form numbers — and treating those as flights
holds every clean submission.
"""

from __future__ import annotations

from .. import manifest as manifest_module
from ..schema import FlightRecord, IssueLevel, Itinerary, TrainRecord

SOURCE = "completeness"


def _transport(records: list) -> list:
    return [r for r in records if isinstance(r, (FlightRecord, TrainRecord))]


def _services_claimed(records: list) -> set[str]:
    claimed = set()
    for record in _transport(records):
        operator = getattr(record, "carrier", None) or getattr(record, "operator", None)
        number = getattr(record, "number", None)
        if operator and number and str(number).isdigit():
            claimed.add(f"{operator.upper()[:2]}{int(number)}")
    return claimed


def _routes_claimed(records: list) -> set[str]:
    claimed = set()
    for record in _transport(records):
        origin, destination = record.origin.iata, record.destination.iata
        if origin and destination:
            claimed.add(f"{origin.upper()}-{destination.upper()}")
    return claimed


def missing_journeys(text: str, records: list, barcode_payloads=None, pages: int = 0) -> list[str]:
    """Journeys the document identifies that no record claims.

    Named, not just counted, so the pipeline can go back and ask for the one
    that is missing rather than only warning about it.
    """
    found = manifest_module.read(text, barcode_payloads, pages)
    services_claimed = _services_claimed(records)
    routes_claimed = _routes_claimed(records)

    if not _transport(records):
        # Nothing was extracted to compare against, which is the most serious
        # version of this failure — every leg missing, not one. Reported, but
        # only for a document that is about travelling: "ref AB 1234" on a
        # hotel confirmation would otherwise hold every stay ever submitted.
        if not manifest_module.looks_like_transport(text):
            return []
        return found.named

    missing = []

    # Services: only those sharing a carrier with a record we did extract.
    carriers = {code[:2] for code in services_claimed}
    missing.extend(
        code
        for code in found.signals.get("services", [])
        if code not in services_claimed and code[:2] in carriers
    )

    # Routes, compared by identity where the records carry codes.
    printed_routes = found.signals.get("routes", [])
    if routes_claimed:
        missing.extend(route for route in printed_routes if route not in routes_claimed)
    elif len(printed_routes) > len(_transport(records)):
        # A rail record holds station names, not codes, so its route cannot be
        # matched against "BBY » NYP" by name. The count still can, and that is
        # the only signal that sees a missing leg on a ticket whose service
        # number is bare. It cannot say *which* route is unaccounted for, so it
        # names them all and lets the reader decide.
        missing.extend(printed_routes)

    # Boarding passes state their own leg count, and that is machine-written.
    encoded = manifest_module.barcode_legs(barcode_payloads or [])
    if encoded > len(_transport(records)):
        missing.append(f"{encoded - len(_transport(records))} more leg(s) encoded in the barcode")

    return sorted(set(missing))


def unnamed_journeys(text: str, records: list) -> list:
    """Records claiming a service the document never names.

    The opposite failure to a missing leg, and the one nothing here looked for:
    a reading that produces *more* journeys than the page describes. Measured
    on a four-flight itinerary, one run returned seven records — three of them
    would have gone on a calendar as flights nobody has a seat on.

    Only a record whose carrier the document does use is reported, and only
    when the page named services at all. A number the scan simply missed is
    common; a number for an airline that appears nowhere on the ticket is the
    reading having invented a leg.
    """
    named = manifest_module.designators(text)
    if not named:
        return []

    carriers = {code[:2] for code in named}
    surplus = []
    for record in _transport(records):
        carrier = (
            getattr(record, "carrier", None) or getattr(record, "operator", None) or ""
        ).upper()[:2]
        number = str(getattr(record, "number", "") or "").lstrip("0")
        if not carrier or not number or carrier not in carriers:
            continue
        if f"{carrier}{number}" not in named:
            surplus.append(record)
    return surplus


def run(itinerary: Itinerary, barcode_payloads=None, pages: int = 0) -> Itinerary:
    if not itinerary.source_text:
        return itinerary

    for name, text in itinerary.source_text.items():
        missing = missing_journeys(text, itinerary.records, barcode_payloads, pages)
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

    for name, text in itinerary.source_text.items():
        for record in unnamed_journeys(text, itinerary.records):
            carrier = (
                getattr(record, "carrier", None) or getattr(record, "operator", None) or ""
            )
            record.add_issue(
                IssueLevel.WARN,
                "leg.not_named_in_document",
                f"'{name}' does not mention {carrier}{record.number}, though it names "
                f"other {carrier} services. This may be a leg that was read twice or "
                "invented; check it before adding it.",
                SOURCE,
            )
    return itinerary


#: Kept for callers that only want the named services.
def services_in(text: str) -> list[str]:
    return manifest_module.read(text).named
