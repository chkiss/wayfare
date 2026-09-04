"""Validation pipeline.

Extraction produces a *claim*; validation decides whether that claim is
allowed anywhere near a real calendar. The validators run in a fixed order
because each one depends on the previous having succeeded:

1. ``resolve``   — fill in timezones and coordinates from the airport database.
2. ``repair``    — deterministically fix the known-ambiguous cases (a red-eye
                   whose arrival date the ticket never printed).
3. ``geo``       — is the implied block time physically possible?
4. ``schedule``  — optional: does the flight number really fly at that time?
5. ``coherence`` — do the records agree with each other and with the calendar?
6. ``completeness`` — is a leg the document lists missing altogether?

The last one runs last and asks the opposite question to the others. They all
check a record that exists; it checks for one that should.

A validator never silently edits a value. Anything it changes it records as an
issue, so the review screen can always show what the tool did on its own.
"""

from __future__ import annotations

from ..schema import IssueLevel, Itinerary
from . import coherence, completeness, geo, repair, resolve, schedule

__all__ = ["run_all", "coherence", "completeness", "geo", "repair", "resolve", "schedule"]


def run_all(itinerary: Itinerary, existing_events: list | None = None) -> Itinerary:
    """Run every validator over an itinerary, in order.

    A validator that raises loses its own findings and nothing else. One did:
    a sort key in `coherence` compared an aware datetime to a naive one and
    took four of six correctly-read itineraries down with it, reported to the
    user as "could not be read at all" — a document read perfectly, discarded
    by the code that was supposed to be checking it.

    Checks are not the reason this tool exists; the records are. A crash in a
    check is reported as a check that did not run, which costs the record the
    confidence that check would have earned, and leaves the reading intact.
    """
    for name, run in (
        ("resolve", lambda: resolve.run(itinerary)),
        ("repair", lambda: repair.run(itinerary)),
        ("geo", lambda: geo.run(itinerary)),
        ("schedule", lambda: schedule.run(itinerary)),
        ("coherence", lambda: coherence.run(itinerary, existing_events or [])),
        ("completeness", lambda: completeness.run(itinerary)),
    ):
        try:
            run()
        except Exception as exc:  # noqa: BLE001 - a failed check is not a failed reading
            itinerary.add_issue(
                IssueLevel.WARN,
                "validate.check_failed",
                f"The {name} check could not run ({type(exc).__name__}: {exc}). "
                "Everything it would have caught is unchecked.",
                "validate",
            )
    return itinerary
