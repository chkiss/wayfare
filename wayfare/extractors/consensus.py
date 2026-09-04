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
from . import llm as llm_extractor

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
) -> tuple[list[Record], list[str], list[dict | None]]:
    """Read the document with each model in parallel: (readings, used, conversations).

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
        return [], [], []

    def attempt(model: str):
        try:
            answer = extractor(model, text, source_file, ocr_confidence, **prompt_options)
        except Exception:  # noqa: BLE001 - one model failing is not a failure
            return model, None, None
        # An extractor may hand back the exchange as well as the records, so a
        # follow-up question can be asked in the same conversation.
        if isinstance(answer, tuple):
            return model, answer[0], answer[1]
        return model, answer, None

    readings: list[tuple[str, list[Record], dict | None]] = []
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
                    model, records, conversation = future.result()
                except Exception:  # noqa: BLE001 - attempt() already absorbs these
                    continue
                if records is None:
                    continue  # Refused or failed; a spare may still answer.

                readings.append((model, records, conversation))
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

    return (
        [records for _, records, _ in readings],
        [model for model, _, _ in readings],
        [conversation for _, _, conversation in readings],
    )


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


def reconcile(
    readings: list[list[Record]],
    models: list[str],
    source_text: str | None = None,
    conversation: dict | None = None,
    adjudicator: str | None = None,
) -> list[Record]:
    """Fold several models' readings of one document into one set of records.

    Where the readings differ, the model that did the reading is asked which
    value the document supports, before anything is reported to the user. It
    is asked once for the whole document rather than once per record, because
    the disputes are usually the same misreading repeated down a ticket and
    one round trip to a slow free model is enough to settle all of them.
    """
    if len(readings) < 2:
        return readings[0] if readings else []

    merged: list[Record] = []
    outstanding: list[tuple[Record, list[str], list[dict]]] = []
    for versions in _group(readings):
        record, agreed, disputed = _reconcile_one(versions, len(readings), models)
        merged.append(record)
        outstanding.append((record, agreed, disputed))

    resolved = _ask_the_reader(outstanding, source_text, conversation, adjudicator)

    for record, agreed, disputed in outstanding:
        _report(record, agreed, disputed, len(readings), models, resolved)

    merged.sort(key=lambda r: (_start_of(r).local if _start_of(r) else datetime.max))
    return merged


def _ask_the_reader(
    outstanding: list[tuple[Record, list[str], list[dict]]],
    source_text: str | None,
    conversation: dict | None,
    adjudicator: str | None,
) -> dict[int, str]:
    """Have the model settle what it can, returning the disputes it decided.

    Keyed by the identity of the dispute, because the same field name recurs
    across every record in a document and the ruling belongs to one of them.
    """
    if not (source_text and conversation and adjudicator):
        return {}

    askable = [d for record, _, disputes in outstanding for d in disputes if d["text"]]
    if not askable:
        return {}

    # Asked under the field's own name, so the model is answering about
    # "origin.name" rather than about an index into a list it cannot see.
    # Identical questions from different records collapse into one, and the
    # answer applies to each of them.
    by_question: dict[tuple, list[dict]] = {}
    for dispute in askable:
        by_question.setdefault((dispute["field"], tuple(dispute["values"])), []).append(dispute)

    questions = [
        {"field": field, "values": list(values)} for field, values in by_question
    ]

    try:
        picked = llm_extractor.adjudicate(adjudicator, conversation, questions, source_text)
    except Exception:  # noqa: BLE001 - an unsettled dispute is the status quo
        return {}

    resolved: dict[int, str] = {}
    for (field, values), disputes in by_question.items():
        chosen = picked.get(field)
        if chosen is None:
            continue
        for dispute in disputes:
            holder = dispute["holder"]
            if getattr(holder, dispute["attr"], None) != chosen:
                setattr(holder, dispute["attr"], chosen)
            resolved[id(dispute)] = chosen
    return resolved


def _report(
    record: Record,
    agreed: list[str],
    disputed: list[dict],
    model_count: int,
    models: list[str],
    resolved: dict[int, str],
) -> None:
    """Say what the readings did and did not settle between them."""
    settled = [d for d in disputed if id(d) in resolved]
    open_disputes = [d for d in disputed if id(d) not in resolved]

    if agreed:
        distinct = len(set(models))
        who = (
            f"{model_count} models"
            if distinct >= model_count
            else f"{model_count} readings by {distinct} model"
        )
        record.add_issue(
            IssueLevel.INFO,
            "consensus.models_agree",
            f"{who} read this independently and agreed on "
            f"{len(agreed)} values, including {', '.join(sorted(agreed)[:4])}.",
            SOURCE,
        )

    if settled:
        record.add_issue(
            IssueLevel.INFO,
            "consensus.resolved_by_model",
            "The readings differed about "
            + "; ".join(f"{d['field']} (kept '{resolved[id(d)]}')" for d in settled)
            + ". The model that read the document was asked which the source "
            "supports, and had to quote the line that decided it.",
            SOURCE,
        )

    if open_disputes:
        record.add_issue(
            IssueLevel.WARN,
            "consensus.models_disagree",
            "The readings disagreed about "
            + "; ".join(
                f"{d['field']}: " + " or ".join(f"'{v}'" for v in d["values"])
                for d in open_disputes
            )
            + ". The most fully quoted reading was kept — choose below, or "
            "check these before trusting them.",
            SOURCE,
        )
        record.disputes = [
            {"field": d["field"], "values": list(d["values"]), "chosen": None}
            for d in open_disputes
            if d["text"]
        ]


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


def _reconcile_one(
    versions: list[Record], model_count: int, models: list[str]
) -> tuple[Record, list[str], list[dict]]:
    """One journey as several models read it, with what they did and did not settle."""
    # The reading with the most fields quoted from the document leads, and the
    # others fill its gaps. Extraction confidence is exactly that measure.
    versions = sorted(versions, key=lambda r: r.extraction_confidence, reverse=True)
    best, others = versions[0], versions[1:]

    agreed: list[str] = []
    disputed: list[dict] = []

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
        agreed = []  # Nothing was corroborated; only one reading saw this.

    best.provenance.model = " + ".join(dict.fromkeys(models))
    return best, agreed, disputed


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
            name = f"{prefix}{field}"
            existing = next((d for d in disputed if d["field"] == name), None)
            if existing is None:
                disputed.append(
                    {
                        "field": name,
                        "attr": field,
                        "values": [_show(mine), _show(theirs)],
                        "holder": primary,
                        # Only plain text can be handed back and forth as a
                        # choice. A disputed time is a disputed *datetime*, and
                        # a rendered one cannot be put back on the record.
                        "text": isinstance(mine, str) and isinstance(theirs, str),
                    }
                )
            elif _show(theirs) not in existing["values"]:
                existing["values"].append(_show(theirs))
                existing["text"] = existing["text"] and isinstance(theirs, str)


def _show(value) -> str:
    if isinstance(value, LocalTime):
        return value.local.strftime("%Y-%m-%d %H:%M")
    return str(value)
