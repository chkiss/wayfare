"""End to end: an uploaded file becomes validated, mergeable records.

The merge is the interesting part. Three extractors of very different
reliability look at the same document, and the result should be better than
any of them alone:

* the barcode knows the route, flight number, date and seat exactly, but no times;
* KItinerary knows real document layouts, but only sees original documents;
* the model can read a screenshot's text, but may be wrong about anything.

So records are merged field by field, most trustworthy source first, and every
disagreement between sources is recorded as an issue rather than resolved
silently. A conflict between the barcode and the model is a *finding* — it
usually means the OCR was bad, and it is exactly what the review screen should
show you.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .config import get_config
from .extractors import barcode as barcode_extractor
from .extractors import consensus
from .extractors import kitinerary as kitinerary_extractor
from .extractors import llm as llm_extractor
from . import manifest, progress
from .ingest import Ingested, ingest, ingest_text
from .schema import (
    FlightRecord,
    IssueLevel,
    Itinerary,
    LocalTime,
    Record,
    text_digest,
)
from .validate import completeness, run_all

#: Order of authority. Earlier wins on a field-by-field conflict.
TRUST = {"barcode": 3, "kitinerary": 2, "llm": 1, "manual": 4}

#: Labels the barcode contents where they are appended to the OCR text, so the
#: model can tell machine-written data from what was read off the pixels.
BARCODE_HEADING = "--- barcode contents (machine-written, not OCR) ---"

#: Extra models asked beyond the quorum, so a refusal costs a spare rather than
#: the cross-check. Abandoned as soon as enough readings are in.
SPARE_MODELS = 2


def process_file(path: Path, original_name: str | None = None, existing_events=None) -> Itinerary:
    """Full pipeline for an uploaded file."""
    progress.report(f"Reading the text of {original_name or path.name}")
    ingested = ingest(path, original_name)
    try:
        return _process(ingested, source_path=path, existing_events=existing_events)
    finally:
        ingested.cleanup()


def process_text(text: str, source_name: str = "-", existing_events=None) -> Itinerary:
    """Full pipeline for a pasted snippet."""
    return _process(ingest_text(text, source_name), source_path=None, existing_events=existing_events)


def _read_with_models(
    text: str, ingested: Ingested, itinerary: Itinerary, payloads: list[str]
) -> list[Record]:
    """Read the document, then hold every reading to the numbers on the page."""
    expect = manifest.read(text, payloads, len(ingested.image_paths)).named
    records = _read_with_models_uncorrected(text, ingested, itinerary, payloads, expect)
    _correct_numbers_against_page(records, expect)
    return records


def _read_with_models_uncorrected(
    text: str,
    ingested: Ingested,
    itinerary: Itinerary,
    payloads: list[str],
    expect: list[str],
) -> list[Record]:
    """Read the document, with as many models as the quorum asks for.

    A quorum of one is the old behaviour exactly, chain and all. Above one, the
    models are named and read in parallel, and their answers are reconciled —
    which is worth the second call because the failure that costs most here is
    a value one model simply left out.
    """
    quorum = get_config().llm_quorum
    progress.report("Working out how many journeys are on the page")

    # Before anything that touches the network: choosing models asks the
    # provider which are free, and with no key that is a pointless request on
    # every upload.
    if quorum < 2 or not llm_extractor.available():
        return llm_extractor.extract(
            text, ingested.source_file, ingested.ocr_confidence, expect=expect
        )

    # Spares, because a busy free model refuses instantly rather than slowly,
    # and without them a quorum of two is a quorum of one most of the time.
    models = llm_extractor.usable_models(quorum + SPARE_MODELS)
    if not models:
        return llm_extractor.extract(
            text, ingested.source_file, ingested.ocr_confidence, expect=expect
        )

    if len(models) < quorum:
        # Not enough distinct models available — on a free tier, usually most
        # of them are rate limited at once. Ask the one that is left twice
        # instead. These models are not deterministic even at temperature
        # zero, and the failures being guarded against are exactly that: the
        # same model, on the same text, dropping a field on one run and not
        # the next. Two samples catch that as well as two models would.
        models = (models * quorum)[:quorum]

    progress.report(
        f"Asking {len(models)} models to read it"
        + (f", expecting {len(expect)} journeys" if expect else "")
    )
    readings, used, conversations = consensus.read(
        text,
        ingested.source_file,
        ingested.ocr_confidence,
        models,
        llm_extractor.read_with,
        want=quorum,
        grace_seconds=get_config().llm_quorum_grace,
        expect=expect,
    )

    if not readings:
        # Every named model failed. Fall back to the chain, which knows how to
        # bench them and how to keep going.
        return llm_extractor.extract(
            text, ingested.source_file, ingested.ocr_confidence, expect=expect
        )

    # Asking several models does not guarantee several answers: measured, four
    # were asked and one answered, the rest refusing instantly on their rate
    # limit. Rather than give up the cross-check, ask the model that *did*
    # answer for a second reading. These models are not deterministic even at
    # temperature zero, and the failure being guarded against is exactly that —
    # the same model dropping a field on one run and not the next.
    if len(readings) < quorum and used:
        try:
            again, exchange = llm_extractor.read_with(
                used[0], text, ingested.source_file, ingested.ocr_confidence, expect=expect
            )
            readings.append(again)
            used.append(used[0])
            conversations.append(exchange)
        except Exception:  # noqa: BLE001 - one reading is still a reading
            pass

    if len(readings) < quorum:
        itinerary.add_issue(
            IssueLevel.INFO,
            "consensus.partial",
            f"Only {len(readings)} of {quorum} readings came back; "
            "this was not cross-checked.",
            "pipeline",
        )

    # The first model to answer adjudicates, because it is the one still
    # holding the document and its own reading of it.
    progress.report(f"Comparing {len(readings)} readings")
    for reading in readings:
        _correct_numbers_against_page(reading, expect)
    exchange, adjudicator = next(
        ((c, m) for c, m in zip(conversations, used) if c), (None, None)
    )
    return consensus.reconcile(
        readings,
        used,
        source_text=text,
        conversation=exchange,
        adjudicator=adjudicator,
    )


def _correct_numbers_against_page(records: list[Record], expect: list[str]) -> None:
    """Put the service number back to what the document actually prints.

    The receipt says "S4246". A model reading it as carrier "S4" and number
    "4246" has taken the carrier's digit twice, and the result is a flight
    number that is wrong but entirely plausible: it survives every check, it
    was promoted at 0.95, and it sends you looking for a flight that does not
    exist.

    Nothing here needs a model. The designators were already scanned off the
    page deterministically before anyone read it, so a number that no
    designator supports — where one that differs only by the carrier's own
    digits does — is corrected to the printed one. A number the page does not
    mention at all is left alone: this repairs a misread, it does not
    overwrite a leg the scan happened to miss.
    """
    if not expect:
        return

    printed: dict[str, set[str]] = {}
    for designator in expect:
        match = re.match(r"([A-Z0-9]{2})\s?(\d+)$", designator.strip().upper())
        if match:
            printed.setdefault(match.group(1), set()).add(match.group(2).lstrip("0"))

    for record in records:
        carrier = getattr(record, "carrier", None) or getattr(record, "operator", None)
        number = str(getattr(record, "number", "") or "").strip().lstrip("0")
        if not carrier or not number:
            continue
        candidates = printed.get(carrier.strip().upper()[:3], set())
        if number in candidates:
            continue

        # Only a number the page's own reading is contained in: "246" inside
        # the misread "4246". Anything else is a different service.
        better = [c for c in candidates if c != number and c in number]
        if len(better) != 1:
            continue

        record.number = better[0]
        record.add_issue(
            IssueLevel.INFO,
            "leg.number_corrected_from_page",
            f"The number was read as {number}, but the document prints "
            f"{carrier}{better[0]}. Corrected to what is on the page.",
            "pipeline",
        )


def _second_pass_for_nothing_at_all(
    text: str, ingested: Ingested, itinerary: Itinerary
) -> list[Record]:
    """Ask again when a plainly travel-shaped document yielded nothing.

    Free models occasionally return an empty list for a document they read
    correctly a moment earlier — measured on one Delta receipt: six identical
    calls all succeeded, while the submission that prompted them produced
    nothing at all. Nothing in the itinerary can catch that, because there is
    no record to check.

    The missing-leg pass cannot help here either. It works from services and
    routes the document names, and this email writes neither: "DELTA 273" is
    an airline's full name, and "NYC-KENNEDY" is not a coded route. So the
    trigger is the absence itself, and the only evidence needed to act on it
    is that the document is about travelling.
    """
    if not manifest.looks_like_transport(text):
        return []

    try:
        found = llm_extractor.extract(
            text, ingested.source_file, ingested.ocr_confidence, insist=True
        )
    except Exception:  # noqa: BLE001 - the "nothing found" error still stands
        return []

    if found:
        itinerary.add_issue(
            IssueLevel.INFO,
            "llm.second_pass",
            "The first reading found nothing in a document that describes a journey; "
            f"asking again recovered {len(found)}.",
            "pipeline",
        )
    return found


def _second_pass_for_missing_legs(
    text: str,
    ingested: Ingested,
    candidates: list[Record],
    itinerary: Itinerary,
    payloads: list[str],
) -> list[Record]:
    """Go back for a leg the document lists and the first reading missed.

    A dropped leg is the failure that hides best: it has no fields to check, so
    every validator passes and the itinerary looks perfect. One real receipt
    listed two flights and produced one record, promoted at 90% confidence.

    Asking again is worth it because the second question is a much easier one.
    The first pass is open-ended reading of a jumbled two-column table; this
    one names the flight number to find. It costs a second model call only when
    a deterministic check has already proved something is absent.
    """
    missing = completeness.missing_journeys(
        text, candidates, barcode_payloads=payloads, pages=len(ingested.image_paths)
    )
    if not missing:
        return []

    try:
        found = llm_extractor.extract(
            text, ingested.source_file, ingested.ocr_confidence, only=missing
        )
    except Exception:  # noqa: BLE001 - the warning still stands if this fails
        return []

    if found:
        itinerary.add_issue(
            IssueLevel.INFO,
            "llm.second_pass",
            f"The first reading missed {', '.join(missing)}; asking again for those "
            f"services specifically recovered {len(found)}.",
            "pipeline",
        )
    return found


def _process(
    ingested: Ingested, source_path: Path | None, existing_events=None
) -> Itinerary:
    itinerary = Itinerary()
    candidates: list[Record] = []

    # 1. Barcodes: certain, and available from both images and printed payloads.
    payloads = barcode_extractor.scan_images(ingested.image_paths)
    payloads.extend(barcode_extractor.find_in_text(ingested.text))
    boarding_passes = []
    unparsed: list[str] = []
    for payload in payloads:
        legs = barcode_extractor.parse(payload)
        boarding_passes.extend(legs)
        if not legs:
            unparsed.append(payload)
    barcode_records = barcode_extractor.to_records(boarding_passes, ingested.source_file)
    candidates.extend(barcode_records)

    # A barcode that is not a boarding pass used to be decoded and dropped on
    # the floor. Most rail and coach tickets carry one, and while the payload
    # is not a schema we can parse, it is machine-written text about this exact
    # booking — a reservation number, a service, sometimes the stations. It
    # goes to the model as source text, which also lets the model quote it.
    model_text = ingested.text
    if unparsed:
        model_text = (model_text + "\n\n" + BARCODE_HEADING + "\n" + "\n".join(unparsed)).strip()
        itinerary.add_issue(
            IssueLevel.INFO,
            "barcode.not_a_boarding_pass",
            f"Read {len(unparsed)} barcode{'s' if len(unparsed) > 1 else ''} that "
            "are not IATA boarding passes. Their contents were passed to the "
            "reader as extra source text.",
            "pipeline",
        )

    # 2. KItinerary, when the input is a real document and the tool is present.
    if source_path is not None and kitinerary_extractor.available():
        candidates.extend(kitinerary_extractor.extract(source_path, ingested.source_file))

    # 3. The model, over text only.
    if model_text.strip():
        try:
            # Count the services in the document before anyone reads it. The
            # model then has a checklist rather than an open-ended page, which
            # is what stops a leg being dropped instead of catching it after.
            candidates.extend(
                _read_with_models(model_text, ingested, itinerary, payloads)
            )
            candidates.extend(
                _second_pass_for_missing_legs(
                    model_text, ingested, candidates, itinerary, payloads
                )
            )
            if not candidates:
                candidates.extend(_second_pass_for_nothing_at_all(model_text, ingested, itinerary))
        except llm_extractor.LLMUnavailable as exc:
            itinerary.add_issue(
                IssueLevel.WARN,
                "llm.unavailable",
                f"Model extraction was skipped: {exc}",
                "pipeline",
            )
        except Exception as exc:  # noqa: BLE001 - a provider failure must not lose the barcode data
            itinerary.add_issue(
                IssueLevel.WARN,
                "llm.failed",
                f"Model extraction failed ({type(exc).__name__}); "
                "only deterministic extractors contributed.",
                "pipeline",
            )

    if not candidates:
        itinerary.add_issue(
            IssueLevel.ERROR,
            "extract.nothing_found",
            f"No travel booking could be extracted from '{ingested.source_file}' "
            f"(read via {ingested.method}).",
            "pipeline",
        )
        # Especially here: "nothing found" is the case where seeing what the
        # reader actually got is the whole diagnosis.
        itinerary.source_text[ingested.source_file] = model_text
        return itinerary

    itinerary.records = _merge(candidates, itinerary)

    digest = text_digest(ingested.text)
    for record in itinerary.records:
        record.provenance.text_sha256 = digest
        if record.provenance.ocr_confidence is None:
            record.provenance.ocr_confidence = ingested.ocr_confidence

    if ingested.ocr_confidence is not None and ingested.ocr_confidence < 0.75:
        itinerary.add_issue(
            IssueLevel.WARN,
            "ocr.low_confidence",
            f"OCR confidence was only {ingested.ocr_confidence:.0%}"
            + (
                f" (doubtful: {', '.join(ingested.doubtful_words[:8])})"
                if ingested.doubtful_words
                else ""
            )
            + ". Check every value before trusting it.",
            "pipeline",
        )

    itinerary.source_text[ingested.source_file] = model_text
    progress.report(f"Checking what was read from {ingested.source_file}")
    return run_all(itinerary, existing_events)


# --- merging -------------------------------------------------------------


def _same_journey(a: Record, b: Record) -> bool:
    """Do two records describe the same booking?"""
    if a.kind is not b.kind:
        return False

    if isinstance(a, FlightRecord) and isinstance(b, FlightRecord):
        if a.carrier and b.carrier and a.number and b.number:
            if (a.carrier, a.number) != (b.carrier, b.number):
                return False
            return abs((a.departure.local.date() - b.departure.local.date()).days) <= 1
        if a.origin.iata and b.origin.iata and a.destination.iata and b.destination.iata:
            return (
                a.origin.iata == b.origin.iata
                and a.destination.iata == b.destination.iata
                and a.departure.local.date() == b.departure.local.date()
            )
        return False

    start_a = getattr(a, "departure", None) or getattr(a, "check_in", None) or getattr(a, "start", None)
    start_b = getattr(b, "departure", None) or getattr(b, "check_in", None) or getattr(b, "start", None)
    if start_a is None or start_b is None:
        return False
    return start_a.local.date() == start_b.local.date()


def _is_placeholder_time(when: LocalTime | None) -> bool:
    """Midnight from a barcode means 'time not stated', not 'departs at 00:00'."""
    return isinstance(when, LocalTime) and when.local.time() == datetime.min.time()


def _merge_pair(primary: Record, secondary: Record, itinerary: Itinerary) -> Record:
    """Fold `secondary` into the more trustworthy `primary`."""
    primary_trust = TRUST.get(primary.provenance.extractor, 0)
    secondary_trust = TRUST.get(secondary.provenance.extractor, 0)

    for field in primary.model_fields:
        if field in {"kind", "issues", "provenance", "extraction_confidence"}:
            continue

        primary_value = getattr(primary, field, None)
        secondary_value = getattr(secondary, field, None)
        if secondary_value in (None, ""):
            continue

        # The barcode's midnight is a placeholder; a real time always beats it.
        if _is_placeholder_time(primary_value) and isinstance(secondary_value, LocalTime):
            merged = LocalTime(
                local=datetime.combine(primary_value.local.date(), secondary_value.local.time()),
                timezone=secondary_value.timezone,
            )
            setattr(primary, field, merged)
            continue

        if primary_value in (None, ""):
            setattr(primary, field, secondary_value)
            continue

        if primary_value != secondary_value and _is_scalar(primary_value):
            if primary_trust > secondary_trust:
                primary.add_issue(
                    IssueLevel.WARN,
                    "merge.source_conflict",
                    f"'{field}': {primary.provenance.extractor} says "
                    f"'{primary_value}', {secondary.provenance.extractor} says "
                    f"'{secondary_value}'. Kept the more reliable source.",
                    "pipeline",
                )

    primary.issues.extend(
        issue for issue in secondary.issues if issue.code != "barcode.time_not_encoded"
    )
    primary.provenance.extractor = "+".join(
        dict.fromkeys([primary.provenance.extractor, secondary.provenance.extractor])
    )
    primary.extraction_confidence = max(
        primary.extraction_confidence, secondary.extraction_confidence
    )
    return primary


def _is_scalar(value) -> bool:
    return isinstance(value, (str, int, float, bool))


def _merge(candidates: list[Record], itinerary: Itinerary) -> list[Record]:
    ordered = sorted(
        candidates,
        key=lambda r: TRUST.get(r.provenance.extractor, 0),
        reverse=True,
    )
    merged: list[Record] = []
    for candidate in ordered:
        for existing in merged:
            if _same_journey(existing, candidate):
                _merge_pair(existing, candidate, itinerary)
                break
        else:
            merged.append(candidate)

    # A barcode record that never found a time is still worth keeping, but the
    # user has to be told the hour is missing rather than 00:00.
    for record in merged:
        if isinstance(record, FlightRecord) and _is_placeholder_time(record.departure):
            record.add_issue(
                IssueLevel.WARN,
                "flight.no_departure_time",
                "The barcode gave the route and date but no time could be read from the "
                "document, so this is currently set to midnight. Set the time before "
                "promoting it.",
                "pipeline",
            )
    return merged
