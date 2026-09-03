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

A validator never silently edits a value. Anything it changes it records as an
issue, so the review screen can always show what the tool did on its own.
"""

from __future__ import annotations

from ..schema import Itinerary
from . import coherence, geo, repair, resolve, schedule

__all__ = ["run_all", "coherence", "geo", "repair", "resolve", "schedule"]


def run_all(itinerary: Itinerary, existing_events: list | None = None) -> Itinerary:
    """Run every validator over an itinerary, in order."""
    resolve.run(itinerary)
    repair.run(itinerary)
    geo.run(itinerary)
    schedule.run(itinerary)
    coherence.run(itinerary, existing_events or [])
    return itinerary
