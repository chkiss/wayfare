"""Submissions on disk, and the promotion decision.

A submission is one upload and everything that came of it: the records, their
issues, and the calendar events that were written. It is kept as a single JSON
file so the review screen, the CLI and the agent API all see the same state,
with no database to run.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .calendar_api import CalendarClient
from .config import get_config
from .icswrite import record_to_ics
from .render import event_summary, load_conventions, to_google_events
from .schema import Itinerary, Record, RecordAdapter


@dataclass
class RecordOutcome:
    """What happened to one record."""

    summary: str
    confidence: float
    #: "promoted" (on the real calendar), "pending" (quarantined), "rejected".
    status: str
    reason: str
    event_ids: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    #: The whole event as iCalendar text. Every value the tool concluded, in
    #: the one place a reviewer can read it end to end and paste elsewhere —
    #: the summary line alone hides the times, zones, location and reminders.
    ics: str = ""
    #: Values two readings disagreed about that nothing could settle, offered
    #: to the reviewer as a choice rather than only as a warning.
    disputes: list[dict] = field(default_factory=list)
    #: The record as the pipeline concluded it. Kept so a correction made
    #: later is re-rendered by the code that rendered it first, instead of
    #: being patched into the finished strings.
    record: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "confidence": self.confidence,
            "status": self.status,
            "reason": self.reason,
            "event_ids": self.event_ids,
            "issues": self.issues,
            "ics": self.ics,
            "disputes": self.disputes,
            "record": self.record,
        }


@dataclass
class Submission:
    submission_id: str
    created: str
    source_file: str
    outcomes: list[RecordOutcome] = field(default_factory=list)
    itinerary_issues: list[dict] = field(default_factory=list)
    #: What the extractors were given, per document. The uploaded file itself
    #: is never kept; this is what makes a wrong field diagnosable.
    source_text: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "created": self.created,
            "source_file": self.source_file,
            "itinerary_issues": self.itinerary_issues,
            "source_text": self.source_text,
            "records": [o.to_dict() for o in self.outcomes],
            "summary": {
                "promoted": sum(1 for o in self.outcomes if o.status == "promoted"),
                "pending": sum(1 for o in self.outcomes if o.status == "pending"),
                "rejected": sum(1 for o in self.outcomes if o.status == "rejected"),
            },
        }


def decide(record: Record, allow_promote: bool = True) -> tuple[str, str]:
    """Should this record reach the real calendar? Returns (status, reason)."""
    cfg = get_config()
    confidence = record.confidence()

    if record.errors:
        return "rejected", record.errors[0].message

    if not allow_promote or not cfg.auto_promote:
        return "pending", "Automatic promotion is disabled; held for review."

    if record.warnings:
        # The warnings are listed in full directly underneath this line, so
        # quoting the first one here just printed the same sentence twice.
        count = len(record.warnings)
        return "pending", f"Held for review — {count} thing{'s' if count > 1 else ''} to check:"

    if confidence < cfg.promote_threshold:
        return (
            "pending",
            f"Confidence {confidence:.0%} is below the {cfg.promote_threshold:.0%} "
            "threshold; held for review.",
        )

    return "promoted", f"All checks passed (confidence {confidence:.0%})."


def _terse(exc: Exception) -> str:
    """A provider error reduced to the sentence a person can act on.

    A googleapiclient HttpError stringifies to the full request URL and a JSON
    error body; the useful part is the message inside it.
    """
    text = str(exc)
    match = re.search(r'returned "([^"]+)"', text)
    if match:
        return match.group(1)
    return text[:160]


def commit(
    itinerary: Itinerary,
    source_file: str,
    client: CalendarClient | None = None,
    allow_promote: bool = True,
    dry_run: bool = False,
) -> Submission:
    """Write an itinerary to the calendar under the promotion policy."""
    cfg = get_config()
    cfg.ensure_dirs()
    conventions = load_conventions()

    submission = Submission(
        submission_id=uuid.uuid4().hex[:12],
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_file=source_file,
        itinerary_issues=[i.model_dump(mode="json") for i in itinerary.issues],
        source_text=dict(itinerary.source_text),
    )

    client = client or (None if dry_run else CalendarClient())

    # A record whose own zone could not be resolved is written in the
    # calendar's zone rather than the server's, which is a fact about where the
    # box is hosted and nothing to do with the person reading the event.
    if client is not None and not conventions.get("default_timezone"):
        zone = getattr(client, "calendar_timezone", lambda: None)()
        if zone:
            conventions["default_timezone"] = zone

    for record in itinerary.records:
        status, reason = decide(record, allow_promote)
        outcome = RecordOutcome(
            summary=event_summary(record, conventions),
            confidence=record.confidence(),
            status=status,
            reason=reason,
            issues=[i.model_dump(mode="json") for i in record.issues],
            ics=record_to_ics(record, conventions, uid_seed=submission.submission_id),
            disputes=[dict(d) for d in record.disputes],
            record=record.model_dump(mode="json"),
        )

        if status != "rejected" and not dry_run and client is not None:
            try:
                pending_id = client.pending_calendar_id()
                for body in to_google_events(record, conventions):
                    event = client.create(
                        body, pending_id, colour_id=conventions.get("pending_color_id")
                    )
                    event_id = event["id"]
                    if status == "promoted":
                        client.move(event_id, pending_id, client.target_calendar_id())
                    outcome.event_ids.append(event_id)
            except Exception as exc:  # noqa: BLE001 - one record's write, not the batch's
                # A submission is a whole trip. One record Google will not
                # accept must not discard the legs either side of it, and the
                # user has to be told which one failed and why.
                outcome.status = "rejected"
                outcome.reason = f"Google rejected this event: {_terse(exc)}"
                outcome.issues.append(
                    {
                        "level": "error",
                        "code": "calendar.write_failed",
                        "message": outcome.reason,
                        "source": "store",
                    }
                )

        submission.outcomes.append(outcome)

    if not dry_run:
        _save(submission)
    return submission


def _save(submission: Submission) -> Path:
    cfg = get_config()
    cfg.ensure_dirs()
    path = cfg.records_dir / f"{submission.submission_id}.json"
    path.write_text(json.dumps(submission.to_dict(), indent=2), encoding="utf-8")
    return path


def load(submission_id: str) -> dict | None:
    path = get_config().records_dir / f"{submission_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def note_duration(submission_id: str, seconds: float) -> None:
    """Record how long this submission took to read.

    Kept so the waiting page can be told what a normal wait looks like here,
    measured on this machine with these models, rather than working from a
    number somebody guessed.
    """
    cfg = get_config()
    data = load(submission_id)
    if data is None:
        return
    data["duration_seconds"] = round(seconds, 1)
    (cfg.records_dir / f"{submission_id}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


#: Used until enough submissions have been timed to say anything. Deliberately
#: short: it is how long a page waits before deciding a result is not coming.
DEFAULT_WAIT_SECONDS = 5.0


def typical_duration(sample: int = 20) -> float:
    """How long a submission usually takes, from the ones actually measured.

    The slowest of the recent ones rather than the average: this number is a
    deadline for waiting, and a deadline set at the average is missed half the
    time. Bounded at both ends, because one 40-second outlier should not make
    every future page wait 40 seconds.
    """
    cfg = get_config()
    if not cfg.records_dir.exists():
        return DEFAULT_WAIT_SECONDS

    paths = sorted(cfg.records_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    seen: list[float] = []
    for path in paths[: sample * 2]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data.get("duration_seconds"), (int, float)):
            seen.append(float(data["duration_seconds"]))
        if len(seen) >= sample:
            break

    if len(seen) < 3:
        return DEFAULT_WAIT_SECONDS
    seen.sort()
    ninetieth = seen[min(len(seen) - 1, int(len(seen) * 0.9))]
    return max(3.0, min(30.0, ninetieth))


def latest_submission(since: str | None = None) -> dict | None:
    """The newest submission, optionally only if it is newer than `since`.

    What answers "did the batch I lost track of actually finish?".
    """
    cfg = get_config()
    if not cfg.records_dir.exists():
        return None
    paths = sorted(cfg.records_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:5]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if since and str(data.get("created", "")) < since:
            continue
        return data
    return None


def recent(limit: int = 25, include_discarded: bool = False) -> list[dict]:
    """Recent submissions, without the records you have already dealt with.

    Discarding is the answer to "this is wrong, take it away". Leaving the card
    on screen afterwards means the review list only ever grows, and the one
    thing the user asked for — for it to go — is the one thing that did not
    happen. The record stays on disk either way, so nothing is lost.
    """
    cfg = get_config()
    if not cfg.records_dir.exists():
        return []
    paths = sorted(cfg.records_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    out: list[dict] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue

        # The position in the stored list is what promote and discard address,
        # so it has to survive any filtering of that list.
        for position, record in enumerate(data.get("records", [])):
            record["index"] = position

        if not include_discarded:
            data["records"] = [r for r in data.get("records", []) if r.get("status") != "discarded"]
            # A submission whose every record was discarded has nothing left
            # to show, and its heading alone would be a row of noise.
            if not data["records"]:
                continue

        out.append(data)
        if len(out) >= limit:
            break
    return out


def promote(submission_id: str, index: int, client: CalendarClient | None = None) -> dict:
    """Move a held record's events onto the real calendar after human review."""
    cfg = get_config()
    data = load(submission_id)
    if data is None:
        raise KeyError(f"No such submission: {submission_id}")
    try:
        record = data["records"][index]
    except (IndexError, KeyError) as exc:
        raise KeyError(f"No record {index} in submission {submission_id}") from exc

    if record["status"] == "promoted":
        return record

    client = client or CalendarClient()
    pending_id = client.pending_calendar_id()
    for event_id in record.get("event_ids", []):
        client.move(event_id, pending_id, client.target_calendar_id())

    record["status"] = "promoted"
    record["reason"] = "Promoted manually after review."
    (cfg.records_dir / f"{submission_id}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    return record


class AmendError(RuntimeError):
    """Raised when a record cannot be corrected in place."""


def amend(
    submission_id: str,
    index: int,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    client: CalendarClient | None = None,
) -> dict:
    """Correct a held record's title or times before it is promoted.

    This is what makes "held for review" a decision rather than a dead end.
    A boarding-pass barcode gives a route and a date but no time, and telling
    someone to fix that in Google Calendar and then come back would defeat the
    point of reviewing it here.

    Times are supplied as naive local strings ("2026-03-04T09:35"); the zone
    already on the event is kept, because that zone came from the airport
    database and is more likely right than anything retyped by hand.
    """
    cfg = get_config()
    data = load(submission_id)
    if data is None:
        raise KeyError(f"No such submission: {submission_id}")
    try:
        record = data["records"][index]
    except (IndexError, KeyError) as exc:
        raise KeyError(f"No record {index} in submission {submission_id}") from exc

    event_ids = record.get("event_ids", [])
    if not event_ids:
        raise AmendError("This record has no calendar event to correct.")
    if len(event_ids) > 1:
        raise AmendError(
            "This record was written as two events (check-in and check-out). "
            "Edit those directly in your calendar."
        )

    client = client or CalendarClient()
    calendar_id = (
        client.target_calendar_id()
        if record["status"] == "promoted"
        else client.pending_calendar_id()
    )

    existing = client.get_event(event_ids[0], calendar_id)
    changes: dict = {}
    changed: list[str] = []

    if summary and summary.strip() and summary.strip() != record.get("summary"):
        changes["summary"] = summary.strip()
        changed.append("title")

    for field, value in (("start", start), ("end", end)):
        if not value or not value.strip():
            continue
        stamp = _normalise_local(value)
        if stamp is None:
            raise AmendError(f"'{value}' is not a date and time I can read.")
        existing_field = existing.get(field, {})
        payload = {"dateTime": stamp}
        # Keep the zone the validators resolved rather than the browser's.
        if existing_field.get("timeZone"):
            payload["timeZone"] = existing_field["timeZone"]
        changes[field] = payload
        changed.append(field)

    if not changes:
        return record

    if "start" in changes and "end" not in changes:
        # Google rejects a start that lands after the existing end.
        end_stamp = (existing.get("end") or {}).get("dateTime")
        if end_stamp and end_stamp[:19] < changes["start"]["dateTime"]:
            changes["end"] = dict(changes["start"])
            changed.append("end")

    updated = client.patch(event_ids[0], calendar_id, changes)

    record["summary"] = updated.get("summary", record["summary"])
    record["reason"] = "Corrected by hand (" + ", ".join(changed) + ")."
    record["issues"] = [
        issue
        for issue in record.get("issues", [])
        # The warnings that asked for exactly this edit no longer apply.
        if issue.get("code") not in {"flight.no_departure_time", "leg.no_arrival"}
    ]
    record["edited"] = True
    (cfg.records_dir / f"{submission_id}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    return record


def resolve_dispute(
    submission_id: str,
    index: int,
    field_name: str,
    value: str,
    client: CalendarClient | None = None,
) -> dict:
    """Settle a disagreement the readings and the model both left open.

    The last resort, and the only one with a person in it. Two readings
    differed, the model that read the document could not point to a line that
    decided it, so the question goes to whoever is holding the ticket — who
    can simply look.

    The choice is restricted to the values that were actually offered. This is
    a tie-break between two readings of a document, not a free text field, and
    a value neither reading produced would have no evidence behind it at all.
    """
    cfg = get_config()
    data = load(submission_id)
    if data is None:
        raise KeyError(f"No such submission: {submission_id}")
    try:
        stored = data["records"][index]
    except (IndexError, KeyError) as exc:
        raise KeyError(f"No record {index} in submission {submission_id}") from exc

    dispute = next(
        (d for d in stored.get("disputes", []) if d.get("field") == field_name), None
    )
    if dispute is None:
        raise AmendError(f"Nothing was disputed about {field_name}.")
    if value not in dispute.get("values", []):
        raise AmendError("That was not one of the readings on offer.")
    if not stored.get("record"):
        raise AmendError("This record was saved before choices could be applied.")

    record = RecordAdapter.validate_python(stored["record"])
    holder = record
    *path, attribute = field_name.split(".")
    for step in path:
        holder = getattr(holder, step, None)
        if holder is None:
            raise AmendError(f"{field_name} is not part of this record any more.")
    setattr(holder, attribute, value)

    # Re-rendered rather than patched: the title, the location and the
    # description are all derived from this field, and editing the finished
    # strings would leave them disagreeing with each other.
    conventions = load_conventions()
    stored["summary"] = event_summary(record, conventions)
    stored["ics"] = record_to_ics(record, conventions, uid_seed=submission_id)
    stored["record"] = record.model_dump(mode="json")
    dispute["chosen"] = value

    if all(d.get("chosen") for d in stored.get("disputes", [])):
        # Every open question answered; the warning that asked them is spent.
        stored["issues"] = [
            issue
            for issue in stored.get("issues", [])
            if issue.get("code") != "consensus.models_disagree"
        ]
    stored["edited"] = True

    event_ids = stored.get("event_ids", [])
    if event_ids:
        client = client or CalendarClient()
        calendar_id = (
            client.target_calendar_id()
            if stored["status"] == "promoted"
            else client.pending_calendar_id()
        )
        bodies = to_google_events(record, conventions)
        for event_id, body in zip(event_ids, bodies):
            client.patch(
                event_id,
                calendar_id,
                {
                    key: body[key]
                    for key in ("summary", "location", "description")
                    if key in body
                },
            )

    (cfg.records_dir / f"{submission_id}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    return stored


def _normalise_local(value: str) -> str | None:
    """Accept what a browser's datetime-local input sends, and a bit more."""
    text = value.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def discard(submission_id: str, index: int, client: CalendarClient | None = None) -> dict:
    """Delete a held record's events entirely."""
    cfg = get_config()
    data = load(submission_id)
    if data is None:
        raise KeyError(f"No such submission: {submission_id}")
    record = data["records"][index]

    client = client or CalendarClient()
    calendar_id = (
        client.target_calendar_id()
        if record["status"] == "promoted"
        else client.pending_calendar_id()
    )
    for event_id in record.get("event_ids", []):
        try:
            client.delete(event_id, calendar_id)
        except Exception:  # noqa: BLE001 - already gone is fine
            pass

    record["status"] = "discarded"
    record["reason"] = "Discarded after review."
    record["event_ids"] = []
    (cfg.records_dir / f"{submission_id}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    return record
