"""Several documents, one trip.

Travel does not arrive one file at a time. A return flight is two confirmations,
a trip is usually those two plus a hotel, and the checks that matter most are
precisely the ones that need all of them at once: does the hotel cover the
nights you are actually there, does the return leave after you check out, does
the second leg depart after the first one lands.

Processing each upload as its own submission would throw all of that away —
every file would look internally consistent, and the trip could still be
nonsense. So a batch is extracted per file (each document gets the full
barcode/KItinerary/model treatment) and then combined into a single itinerary
that the cross-record validators see whole.

Combining is deliberately more cautious than the within-document merge in
``pipeline``. Inside one document, two records on the same date are almost
certainly the same booking read twice. Across documents they are usually two
different bookings, so only an unambiguous match — same flight number, same
day — is folded together.
"""

from __future__ import annotations

from datetime import datetime

from .pipeline import TRUST, _merge_pair
from .render import start_local
from .schema import FlightRecord, Itinerary, Record, TrainRecord
from .validate import coherence

#: Sorts a record with no usable time to the end rather than breaking the sort.
_FAR_FUTURE = datetime.max


def describe(sources: list[str]) -> str:
    """What to call a submission assembled from several documents."""
    if len(sources) == 1:
        return sources[0]
    shown = ", ".join(sources[:3])
    return f"{len(sources)} documents ({shown}{', …' if len(sources) > 3 else ''})"


def _designator(record: Record) -> tuple | None:
    """The one identity strong enough to merge two *separate* documents.

    A flight number plus a date is unique. Anything weaker — a route, a date,
    a hotel name — is not: two files describing the same day are ordinarily
    the outbound and the return, not one leg twice.
    """
    if not isinstance(record, (FlightRecord, TrainRecord)):
        return None
    number = getattr(record, "number", None)
    operator = getattr(record, "carrier", None) or getattr(record, "operator", None)
    if not (number and operator):
        return None
    return (record.kind, operator.upper(), number, record.departure.local.date())


def _dedupe_issues(holder) -> None:
    """Drop issues repeated by a second pass of the same validator.

    The cross-document pass re-runs checks that already ran per file, so a
    single-document batch would otherwise show every finding twice.
    """
    seen = set()
    keep = []
    for issue in holder.issues:
        key = (issue.code, issue.message, issue.source)
        if key in seen:
            continue
        seen.add(key)
        keep.append(issue)
    holder.issues = keep


def _sort_key(record: Record):
    when = start_local(record)
    return when.local if when is not None else _FAR_FUTURE


def combine(itineraries: list[Itinerary], existing_events: list | None = None) -> Itinerary:
    """Fold several per-document itineraries into one trip and re-check it."""
    combined = Itinerary()

    for itinerary in itineraries:
        combined.issues.extend(itinerary.issues)

        for candidate in itinerary.records:
            identity = _designator(candidate)
            position = None
            if identity is not None:
                position = next(
                    (i for i, r in enumerate(combined.records) if _designator(r) == identity),
                    None,
                )

            if position is None:
                combined.records.append(candidate)
                continue

            # Same leg, two documents. Keep whichever source is more reliable
            # as the base, so a conflict resolves the same way it would inside
            # a single file.
            held = combined.records[position]
            if TRUST.get(candidate.provenance.extractor, 0) > TRUST.get(
                held.provenance.extractor, 0
            ):
                combined.records[position] = _merge_pair(candidate, held, combined)
            else:
                _merge_pair(held, candidate, combined)

    # Chronological, so the review screen reads as an itinerary rather than as
    # the order the files happened to appear in the file dialog.
    combined.records.sort(key=_sort_key)

    coherence.run(combined, existing_events or [])
    for record in combined.records:
        _dedupe_issues(record)
    _dedupe_issues(combined)
    return combined
