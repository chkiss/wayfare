"""Reading a document with more than one model, and comparing the answers.

Every other defence in this pipeline checks a reading against the document.
This one checks a reading against a second reading, which catches the failure
the others cannot: a model that is *consistent* with the page and simply
incomplete. Measured on real documents, that is the dominant failure —

* a Delta receipt read correctly six times in a row and returned nothing at
  all on the run that mattered;
* the same receipt, read repeatedly, dropped the flight number on one run in
  four and produced "LIS → New York (DL )";
* a SATA receipt listing two flights returned one.

None of those is a wrong value that verification could catch. They are absent
values, and a second model that did not happen to drop the same one fills them
in. Two readings also give something no single reading can: agreement, which
is the only positive evidence available here — everything else in the pipeline
can confirm that a record is *not contradicted*.

The rule is that agreement adds and disagreement warns; nothing is ever
silently chosen between. Where two models differ on a value, both go in front
of the user, because a genuine conflict is exactly the case where a human
glance is worth more than any tie-break rule I could invent.
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime

from ..schema import FlightRecord, IssueLevel, LocalTime, Record, TrainRecord

SOURCE = "consensus"

#: A second opinion may take this much longer than the first reading did, up to
#: the configured ceiling. Enough for an ordinary difference in model speed,
#: short enough that a stalled one is abandoned rather than waited out.
GRACE_MULTIPLIER = 1.5
#: A floor, so a first answer that arrives in a second does not leave the rest
#: no time at all.
MIN_GRACE_SECONDS = 6.0

#: Fields worth comparing between models. Deliberately not everything: issues,
#: provenance and confidence are properties of the reading rather than of the
#: booking, and comparing them would report every record as disputed.
_COMPARED = (
    "carrier",
    "operator",
    "number",
    "departure",
    "arrival",
    "check_in",
    "check_out",
    "start",
    "end",
    "seat",
    "cabin",
    "coach",
    "room",
    "confirmation",
    "traveller",
    "property_name",
    "title",
)

#: Sub-fields of a place, compared the same way.
_PLACE_FIELDS = ("iata", "name", "city", "detail", "address")


def read(
    text: str,
    source_file: str,
    ocr_confidence: float | None,
    models: list[str],
    extractor,
    want: int = 2,
    grace_seconds: float = 25.0,
    **prompt_options,
) -> tuple[list[Record], list[str]]:
    """Read the document with each model, in parallel. Returns (readings, used).

    In parallel because these are independent HTTP calls to a slow provider,
    and running them one after another would multiply the wait for somebody
    watching a spinner.

    Parallel still means waiting for the slowest, though, so a second opinion
    gets only a bounded extra window once the first answer is in. Free models
    vary from seconds to minutes, and a cross-check is worth some delay but not
    an unbounded one — a reading that arrives after the deadline is dropped and
    the record is simply not cross-checked, which is what happens anyway when
    only one model is available.

    More models are asked than answers are wanted, because on a free tier a
    model is far more likely to refuse instantly than to answer slowly —
    measured: a two-model quorum returned one reading in nine seconds, the
    other having been rate limited at once. Spares cost nothing when the first
    choices answer, since the extra requests are abandoned as soon as enough
    readings are in.

    A model that fails is absent from the result. Its failure is the chain's
    business, not this function's: one model being rate limited must not lose
    the answer another one gave.
    """
    if not models:
        return [], []

    def attempt(model: str):
        try:
            return model, extractor(model, text, source_file, ocr_confidence, **prompt_options)
        except Exception:  # noqa: BLE001 - one model failing is not a failure
            return model, None

    readings: list[tuple[str, list[Record]]] = []
    started = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=len(models))
    try:
        pending = {pool.submit(attempt, model) for model in models}
        deadline = None

        while pending and len(readings) < want:
            # Unbounded until something has actually been read, then bounded by
            # what is left of the window.
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                break  # The window expired with the rest still in flight.

            for future in done:
                if len(readings) >= want:
                    break  # Several can land in one batch; take only what was asked for.
                try:
                    model, records = future.result()
                except Exception:  # noqa: BLE001 - attempt() already absorbs these
                    continue
                if records is None:
                    continue  # Refused or failed; a spare may still answer.

                readings.append((model, records))
                # The clock starts at the first *answer*, not the first
                # refusal: a refusal costs no time, and starting it there
                # would spend the window before anybody had read anything.
                #
                # And it is scaled to how long that answer took. Waiting a flat
                # 25 seconds for a second opinion after the first arrived in
                # six means paying four times the reading in hope — measured at
                # 31.8s for a cross-check that never came. A model much slower
                # than the one that already answered is an outlier, not a
                # straggler worth waiting for.
                if deadline is None:
                    taken = time.monotonic() - started
                    allowed = min(grace_seconds, max(MIN_GRACE_SECONDS, taken * GRACE_MULTIPLIER))
                    deadline = time.monotonic() + allowed
    finally:
        # Not waiting on stragglers: their answer is no longer wanted, and
        # somebody is waiting on this.
        pool.shutdown(wait=False, cancel_futures=True)

    return [records for _, records in readings], [model for model, _ in readings]


def _service_identity(record: Record):
    """The strongest identity a record has: its service number and day.

    ``None`` when the number was not read — which is common, and is exactly
    the thing consensus exists to repair, so identity must not depend on it.
    """
    if not isinstance(record, (FlightRecord, TrainRecord)):
        return None
    operator = getattr(record, "carrier", None) or getattr(record, "operator", None)
    number = getattr(record, "number", None)
    if not (operator and number):
        return None
    when = _start_of(record)
    return (record.kind, operator.upper()[:2], str(number).lstrip("0"),
            when.local.date() if when else None)


def _shape_identity(record: Record):
    """A weaker identity that survives any single field being dropped.

    Used to attach a reading that lost its service number to the reading that
    kept it. Two genuinely different flights on one route and day would collide
    here, which is why it is only ever consulted when the stronger identity is
    missing.
    """
    when = _start_of(record)
    day = when.local.date() if when else None

    if isinstance(record, (FlightRecord, TrainRecord)):
        origin = record.origin.iata or record.origin.city or record.origin.name
        destination = record.destination.iata or record.destination.city or record.destination.name
        return (record.kind, str(origin).upper(), str(destination).upper(), day)

    return (record.kind, day)


def _start_of(record: Record) -> LocalTime | None:
    for name in ("departure", "check_in", "start"):
        value = getattr(record, name, None)
        if value is not None:
            return value
    return None


def _comparable(value):
    """A value reduced to something two readings can be compared on."""
    if isinstance(value, LocalTime):
        return value.local.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip().casefold()
    return value


def reconcile(readings: list[list[Record]], models: list[str]) -> list[Record]:
    """Fold several models' readings of one document into one set of records."""
    if len(readings) < 2:
        return readings[0] if readings else []

    merged: list[Record] = []
    for versions in _group(readings):
        merged.append(_reconcile_one(versions, len(readings), models))

    merged.sort(key=lambda r: (_start_of(r).local if _start_of(r) else datetime.max))
    return merged


def _group(readings: list[list[Record]]) -> list[list[Record]]:
    """Collect the versions of each journey across every reading.

    In two passes, because the identities are not equally trustworthy. Records
    that named their service are grouped on that. Records that did not are then
    attached to whichever group has the same shape — same route, same day —
    which is how a reading that dropped the flight number rejoins the one that
    kept it, rather than becoming a second event for the same flight.
    """
    by_service: dict = {}
    unnumbered: list[Record] = []

    for reading in readings:
        for record in reading:
            key = _service_identity(record)
            if key is None:
                unnumbered.append(record)
            else:
                by_service.setdefault(key, []).append(record)

    groups = list(by_service.values())
    shapes = {_shape_identity(group[0]): group for group in groups}

    for record in unnumbered:
        shape = _shape_identity(record)
        if shape in shapes:
            shapes[shape].append(record)
        else:
            new_group = [record]
            groups.append(new_group)
            shapes[shape] = new_group

    return groups


def _reconcile_one(versions: list[Record], model_count: int, models: list[str]) -> Record:
    """One journey as several models read it."""
    # The reading with the most fields quoted from the document leads, and the
    # others fill its gaps. Extraction confidence is exactly that measure.
    versions = sorted(versions, key=lambda r: r.extraction_confidence, reverse=True)
    best, others = versions[0], versions[1:]

    agreed: list[str] = []
    disputed: list[str] = []

    for field in _COMPARED:
        if not hasattr(best, field):
            continue
        _settle(best, others, field, agreed, disputed)

    for place_name in ("origin", "destination", "location"):
        primary = getattr(best, place_name, None)
        if primary is None:
            continue
        for field in _PLACE_FIELDS:
            secondaries = [
                getattr(other, place_name) for other in others if getattr(other, place_name, None)
            ]
            _settle(primary, secondaries, field, agreed, disputed, prefix=f"{place_name}.")

    if len(versions) < model_count:
        best.add_issue(
            IssueLevel.INFO,
            "consensus.one_model_only",
            f"Only {len(versions)} of {model_count} readings found this journey. "
            "It is included because a leg one model missed is still a leg.",
            SOURCE,
        )
    elif agreed:
        best.add_issue(
            IssueLevel.INFO,
            "consensus.models_agree",
            f"{model_count} models read this independently and agreed on "
            f"{len(agreed)} values, including {', '.join(sorted(agreed)[:4])}.",
            SOURCE,
        )

    if disputed:
        best.add_issue(
            IssueLevel.WARN,
            "consensus.models_disagree",
            "The readings disagreed about " + "; ".join(disputed) + ". "
            "The most fully quoted reading was kept — check these before trusting them.",
            SOURCE,
        )

    best.provenance.model = " + ".join(dict.fromkeys(models))
    return best


def _settle(primary, secondaries, field, agreed, disputed, prefix="") -> None:
    """Fill a gap from another reading, or record that the readings differ."""
    mine = getattr(primary, field, None)

    for other in secondaries:
        theirs = getattr(other, field, None)
        if theirs in (None, ""):
            continue

        if mine in (None, ""):
            # The whole point: a value one model dropped and another did not.
            setattr(primary, field, theirs)
            mine = theirs
            agreed.append(f"{prefix}{field} (recovered)")
            continue

        if _comparable(mine) == _comparable(theirs):
            if f"{prefix}{field}" not in agreed:
                agreed.append(f"{prefix}{field}")
        else:
            note = f"{prefix}{field}: '{_show(mine)}' or '{_show(theirs)}'"
            if note not in disputed:
                disputed.append(note)


def _show(value) -> str:
    if isinstance(value, LocalTime):
        return value.local.strftime("%Y-%m-%d %H:%M")
    return str(value)
