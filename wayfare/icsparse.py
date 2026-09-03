"""A small iCalendar reader.

Only enough of RFC 5545 to read an exported calendar: unfold the folded lines,
walk the VEVENTs, unescape the text values. Written by hand rather than pulled
in as a dependency because a calendar export is the one file this tool has to
be able to read on a machine with nothing installed.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

_UNESCAPE = {"\\n": "\n", "\\N": "\n", "\\,": ",", "\\;": ";", "\\\\": "\\"}


@dataclass
class VEvent:
    summary: str = ""
    description: str = ""
    location: str = ""
    start: datetime | date | None = None
    end: datetime | date | None = None
    start_tz: str | None = None
    all_day: bool = False
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def duration_hours(self) -> float | None:
        if not isinstance(self.start, datetime) or not isinstance(self.end, datetime):
            return None
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def duration_days(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        a = self.start if isinstance(self.start, date) else self.start.date()
        b = self.end if isinstance(self.end, date) else self.end.date()
        if isinstance(self.start, datetime):
            a = self.start.date()
        if isinstance(self.end, datetime):
            b = self.end.date()
        return (b - a).days


def unfold(text: str) -> list[str]:
    """RFC 5545 folds long lines by starting the continuation with a space."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    for token, replacement in _UNESCAPE.items():
        value = value.replace(token, replacement)
    return value


def _parse_stamp(value: str) -> tuple[datetime | date | None, bool]:
    value = value.strip()
    for fmt, all_day in (("%Y%m%dT%H%M%S", False), ("%Y%m%dT%H%M%SZ", False)):
        try:
            return datetime.strptime(value, fmt), all_day
        except ValueError:
            continue
    try:
        return datetime.strptime(value, "%Y%m%d").date(), True
    except ValueError:
        return None, False


def parse(text: str) -> list[VEvent]:
    events: list[VEvent] = []
    current: VEvent | None = None

    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            current = VEvent()
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue

        name_part, _, value = line.partition(":")
        name, *params = name_part.split(";")
        name = name.upper()
        current.raw[name] = value

        if name == "SUMMARY":
            current.summary = _unescape(value)
        elif name == "DESCRIPTION":
            current.description = _unescape(value)
        elif name == "LOCATION":
            current.location = _unescape(value)
        elif name in ("DTSTART", "DTEND"):
            stamp, all_day = _parse_stamp(value)
            zone = next(
                (p.split("=", 1)[1] for p in params if p.upper().startswith("TZID=")), None
            )
            if name == "DTSTART":
                current.start, current.all_day, current.start_tz = stamp, all_day, zone
            else:
                current.end = stamp

    return events


def read(path: Path) -> list[VEvent]:
    """Read a .ics file, or a Google Takeout .zip containing several."""
    if path.suffix.lower() == ".zip":
        events: list[VEvent] = []
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".ics"):
                    events.extend(parse(archive.read(name).decode("utf-8", errors="replace")))
        return events
    return parse(path.read_text(encoding="utf-8", errors="replace"))
