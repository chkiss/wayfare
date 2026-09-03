"""The travel record data model.

Everything in the pipeline speaks these types. Extractors produce them,
validators annotate them with issues, and the calendar writer renders them.

Design rule: times are always *naive local time plus an explicit IANA zone*,
never a bare UTC instant. Airline and rail schedules are published in local
time at each end, and collapsing that to UTC too early is the single most
common way an itinerary parser lands the wrong hour on the calendar.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Annotated, ClassVar, Literal, Union

from pydantic import BaseModel, Field


class Kind(str, Enum):
    FLIGHT = "flight"
    TRAIN = "train"
    LODGING = "lodging"
    OTHER = "other"


class IssueLevel(str, Enum):
    #: Worth recording, never blocks promotion.
    INFO = "info"
    #: Something is off. Holds the event in pending for human review.
    WARN = "warn"
    #: The record contradicts itself or physics. Never reaches any calendar.
    ERROR = "error"


class Issue(BaseModel):
    """A single finding from a validator."""

    level: IssueLevel
    #: Stable machine-readable identifier, e.g. "flight.block_time_implausible".
    code: str
    message: str
    #: Which validator produced this.
    source: str = "unknown"

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"[{self.level.value}] {self.code}: {self.message}"


class LocalTime(BaseModel):
    """A wall-clock time together with the zone it is a wall-clock time *in*."""

    #: Naive local datetime, exactly as printed on the ticket.
    local: datetime
    #: IANA zone, e.g. "Europe/Brussels". Resolved from the place when known.
    timezone: str | None = None

    def to_google(self, fallback: str | None = None) -> dict:
        """Google's event time payload.

        A naive dateTime with no timeZone is rejected outright ("Missing time
        zone definition for start time"), so an unresolved zone has to become
        *some* zone at the point of writing. The record keeps its "no timezone"
        warning either way, which is what holds it back for review — the
        fallback makes it writable, not trusted.
        """
        payload = {"dateTime": self.local.isoformat(timespec="seconds")}
        zone = self.timezone or fallback
        if zone:
            payload["timeZone"] = zone
        return payload


class Place(BaseModel):
    """Somewhere an itinerary refers to."""

    name: str | None = None
    #: IATA airport code, uppercased, when this is an airport.
    iata: str | None = None
    #: City the airport or property serves, e.g. "Paris".
    city: str | None = None
    #: Station or terminal detail.
    detail: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None

    def label(self) -> str:
        for candidate in (self.iata, self.name, self.address):
            if candidate:
                return candidate
        return "?"


class Provenance(BaseModel):
    """Where a record came from, so a bad parse can always be traced back."""

    #: Which extractor produced it: "barcode", "kitinerary", "llm", "manual".
    extractor: str
    #: Original filename as uploaded, or "-" for pasted text.
    source_file: str = "-"
    #: SHA-256 of the extracted text, so identical inputs are detectable.
    text_sha256: str | None = None
    #: Mean OCR character confidence, 0..1, when OCR was involved.
    ocr_confidence: float | None = None
    #: Free-form note from the extractor.
    note: str | None = None


class BaseRecord(BaseModel):
    """Fields shared by every kind of travel record."""

    kind: Kind
    #: Booking reference / PNR / confirmation number.
    confirmation: str | None = None
    #: Traveller name as printed, when available.
    traveller: str | None = None
    provenance: Provenance
    #: Extractor's own 0..1 confidence, before validation.
    extraction_confidence: float = 0.5
    issues: list[Issue] = Field(default_factory=list)

    def add_issue(self, level: IssueLevel, code: str, message: str, source: str) -> None:
        self.issues.append(Issue(level=level, code=code, message=message, source=source))

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level is IssueLevel.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level is IssueLevel.WARN]

    #: Findings that positively confirm a record rather than merely failing to
    #: contradict it. Each one earns a little confidence.
    CONFIRMATIONS: ClassVar[set[str]] = {"leg.block_time_ok", "flight.schedule_confirmed"}

    def confidence(self) -> float:
        """Overall confidence after validation.

        Errors floor it at zero; each warning costs a fixed slice; a check that
        actively confirmed the record earns a little back. OCR quality caps the
        result, because no amount of clean reasoning rescues text that was read
        badly in the first place.
        """
        if self.errors:
            return 0.0
        score = self.extraction_confidence - 0.25 * len(self.warnings)
        score += 0.05 * len([i for i in self.issues if i.code in self.CONFIRMATIONS])
        if self.provenance.ocr_confidence is not None:
            score = min(score, self.provenance.ocr_confidence)
        return max(0.0, min(1.0, score))


class FlightRecord(BaseRecord):
    kind: Literal[Kind.FLIGHT] = Kind.FLIGHT
    #: Marketing carrier IATA code, e.g. "BA".
    carrier: str | None = None
    #: Flight number digits only, e.g. "117".
    number: str | None = None
    origin: Place
    destination: Place
    departure: LocalTime
    arrival: LocalTime | None = None
    seat: str | None = None
    cabin: str | None = None
    terminal_departure: str | None = None
    terminal_arrival: str | None = None

    def flight_designator(self) -> str:
        if self.carrier and self.number:
            return f"{self.carrier}{self.number}"
        return self.carrier or self.number or "Flight"


class TrainRecord(BaseRecord):
    kind: Literal[Kind.TRAIN] = Kind.TRAIN
    operator: str | None = None
    number: str | None = None
    origin: Place
    destination: Place
    departure: LocalTime
    arrival: LocalTime | None = None
    coach: str | None = None
    seat: str | None = None


class LodgingRecord(BaseRecord):
    kind: Literal[Kind.LODGING] = Kind.LODGING
    property_name: str | None = None
    location: Place
    check_in: LocalTime
    check_out: LocalTime
    room: str | None = None
    guests: int | None = None


class OtherRecord(BaseRecord):
    kind: Literal[Kind.OTHER] = Kind.OTHER
    title: str
    location: Place | None = None
    start: LocalTime
    end: LocalTime | None = None
    description: str | None = None


Record = Annotated[
    Union[FlightRecord, TrainRecord, LodgingRecord, OtherRecord],
    Field(discriminator="kind"),
]


class Itinerary(BaseModel):
    """Everything extracted from one submission, plus cross-record findings."""

    records: list[Record] = Field(default_factory=list)
    #: Issues that concern the itinerary as a whole rather than one record.
    issues: list[Issue] = Field(default_factory=list)

    def add_issue(self, level: IssueLevel, code: str, message: str, source: str) -> None:
        self.issues.append(Issue(level=level, code=code, message=message, source=source))

    def flights(self) -> list[FlightRecord]:
        return [r for r in self.records if isinstance(r, FlightRecord)]

    def trains(self) -> list[TrainRecord]:
        return [r for r in self.records if isinstance(r, TrainRecord)]

    def lodgings(self) -> list[LodgingRecord]:
        return [r for r in self.records if isinstance(r, LodgingRecord)]


def text_digest(text: str) -> str:
    """Stable digest of extracted text, used to detect duplicate submissions."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
