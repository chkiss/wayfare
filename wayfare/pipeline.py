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

from datetime import datetime
from pathlib import Path

from .extractors import barcode as barcode_extractor
from .extractors import kitinerary as kitinerary_extractor
from .extractors import llm as llm_extractor
from .ingest import Ingested, ingest, ingest_text
from .schema import (
    FlightRecord,
    IssueLevel,
    Itinerary,
    LocalTime,
    Record,
    text_digest,
)
from .validate import run_all

#: Order of authority. Earlier wins on a field-by-field conflict.
TRUST = {"barcode": 3, "kitinerary": 2, "llm": 1, "manual": 4}

#: Labels the barcode contents where they are appended to the OCR text, so the
#: model can tell machine-written data from what was read off the pixels.
BARCODE_HEADING = "--- barcode contents (machine-written, not OCR) ---"


def process_file(path: Path, original_name: str | None = None, existing_events=None) -> Itinerary:
    """Full pipeline for an uploaded file."""
    ingested = ingest(path, original_name)
    try:
        return _process(ingested, source_path=path, existing_events=existing_events)
    finally:
        ingested.cleanup()


def process_text(text: str, source_name: str = "-", existing_events=None) -> Itinerary:
    """Full pipeline for a pasted snippet."""
    return _process(ingest_text(text, source_name), source_path=None, existing_events=existing_events)


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
            candidates.extend(
                llm_extractor.extract(
                    model_text, ingested.source_file, ingested.ocr_confidence
                )
            )
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
