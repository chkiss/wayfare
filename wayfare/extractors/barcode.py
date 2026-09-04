"""IATA BCBP boarding-pass barcodes — the one input that cannot be misread.

A boarding pass barcode carries a fixed-width string written by the airline's
own system: passenger name, PNR, origin, destination, carrier, flight number,
date and seat. There is no OCR involved and no interpretation to get wrong.

What it does *not* carry is a departure time. So a decoded barcode is not a
complete event on its own — it is the reference the rest of the pipeline gets
checked against. If OCR read "LHR" but the barcode says "LHK" is impossible
and "LHR" is certain, the barcode wins.

Reference: IATA Resolution 792, BCBP version 6.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import get_config
from ..schema import (
    FlightRecord,
    IssueLevel,
    LocalTime,
    Place,
    Provenance,
)

#: Mandatory unique section: "M", leg count, 20-char name, e-ticket indicator.
_UNIQUE_LEN = 23
#: Mandatory repeated section per leg, up to and including the conditional size.
_LEG_LEN = 37

_BCBP_RE = re.compile(r"M[1-9][A-Z0-9 /\.\-]{19,}")


@dataclass
class BoardingPass:
    """The facts a barcode states, all of them certain."""

    passenger: str
    pnr: str
    origin: str
    destination: str
    carrier: str
    flight_number: str
    flight_date: date
    seat: str | None
    compartment: str | None

    def designator(self) -> str:
        return f"{self.carrier}{self.flight_number}"


def available() -> bool:
    return shutil.which(get_config().zbarimg_bin) is not None


def _zxing(paths: list[Path]) -> list[str]:
    """Decode with zxing-cpp, which reads the symbologies zbar cannot.

    zbar handles QR and the retail linear codes. It does not decode **Aztec**
    or **PDF417**, and between them those are most of the travel industry: the
    Aztec square is what UIC 918.3 puts on every European rail ticket, and
    PDF417 is what airlines print on paper boarding passes.

    That gap was invisible. zbarimg exits 4 for "no barcode here", which is
    also what it says about a page whose barcode it cannot read, so a rail
    ticket and a blank page looked identical — and the pipeline fell back to
    asking a model to read a document whose exact answer was sitting in a
    square it had skipped. Measured on KItinerary's own boarding-pass sample:
    zbar found nothing at 150, 300 and 600 dpi; zxing read it first time.
    """
    try:
        import zxingcpp
        from PIL import Image
    except ImportError:
        return []

    payloads: list[str] = []
    for path in paths:
        try:
            with Image.open(path) as image:
                for result in zxingcpp.read_barcodes(image.convert("L")):
                    if result.text and result.text.strip():
                        payloads.append(result.text)
        except Exception:  # noqa: BLE001 - an unreadable page is not a failure
            continue
    return payloads


def scan_images(paths: list[Path]) -> list[str]:
    """Decode every barcode found in the given images.

    Both readers are used, because neither covers the other: zbar reads some
    linear symbologies zxing is not built for, and zxing reads the Aztec and
    PDF417 codes that carry every ticket worth having.
    """
    cfg = get_config()
    payloads: list[str] = list(_zxing(paths))

    if available():
        for path in paths:
            proc = subprocess.run(
                [cfg.zbarimg_bin, "--raw", "-q", "--nodbus", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            # zbarimg exits 4 when it simply found nothing; that is not an error.
            if proc.returncode not in (0, 4):
                continue
            payloads.extend(line for line in proc.stdout.splitlines() if line.strip())

    # The same code read by both decoders is one barcode, not two.
    return list(dict.fromkeys(payloads))


def find_in_text(text: str) -> list[str]:
    """Some documents print the BCBP string as text next to the barcode."""
    return [m.group(0) for m in _BCBP_RE.finditer(text)]


def parse(payload: str, reference: date | None = None) -> list[BoardingPass]:
    """Parse a BCBP payload into one entry per leg.

    Returns an empty list rather than raising: a barcode that is not a boarding
    pass (a hotel QR code, a payment code) is an ordinary thing to encounter.
    """
    if len(payload) < _UNIQUE_LEN + _LEG_LEN or not payload.startswith("M"):
        return []

    try:
        leg_count = int(payload[1])
    except ValueError:
        return []

    passenger = _clean_name(payload[2:22])
    cursor = _UNIQUE_LEN
    passes: list[BoardingPass] = []

    for _ in range(leg_count):
        if cursor + _LEG_LEN > len(payload):
            break
        leg = payload[cursor : cursor + _LEG_LEN]

        pnr = leg[0:7].strip()
        origin = leg[7:10].strip().upper()
        destination = leg[10:13].strip().upper()
        carrier = leg[13:16].strip().upper()
        flight_number = leg[16:21].strip()
        julian = leg[21:24].strip()
        compartment = leg[24:25].strip()
        seat = leg[25:29].strip()
        conditional_size_hex = leg[35:37].strip()

        if not (_is_code(origin) and _is_code(destination) and carrier):
            break

        flight_date = _from_julian(julian, reference)
        if flight_date is None:
            break

        passes.append(
            BoardingPass(
                passenger=passenger,
                pnr=pnr,
                origin=origin,
                destination=destination,
                carrier=carrier.rstrip("0123456789 ") or carrier,
                flight_number=flight_number.lstrip("0").rstrip() or flight_number.strip(),
                flight_date=flight_date,
                seat=seat.lstrip("0") or None,
                compartment=compartment or None,
            )
        )

        try:
            conditional_size = int(conditional_size_hex, 16)
        except ValueError:
            conditional_size = 0
        cursor += _LEG_LEN + conditional_size

    return passes


def _is_code(value: str) -> bool:
    return len(value) == 3 and value.isalpha()


def _clean_name(raw: str) -> str:
    name = raw.strip()
    if "/" in name:
        surname, _, given = name.partition("/")
        return f"{given.strip().title()} {surname.strip().title()}".strip()
    return name.title()


def _from_julian(julian: str, reference: date | None = None) -> date | None:
    """Resolve a 3-digit day-of-year to a real date.

    BCBP omits the year, so the year is chosen as the one that puts the flight
    closest to now — looking a little into the past (a pass scanned after the
    trip) and well into the future (a booking made months ahead).
    """
    if not julian.isdigit():
        return None
    day_of_year = int(julian)
    if not 1 <= day_of_year <= 366:
        return None

    today = reference or date.today()
    best: date | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = date(year, 1, 1) + timedelta(days=day_of_year - 1)
        except ValueError:
            continue
        if candidate.timetuple().tm_yday != day_of_year:
            continue  # Day 366 in a non-leap year.
        if best is None or abs((candidate - today).days) < abs((best - today).days):
            best = candidate
    return best


def to_records(passes: list[BoardingPass], source_file: str) -> list[FlightRecord]:
    """Turn decoded passes into flight records with no time yet.

    The date is certain; the time is not stated by the barcode at all, so it is
    set to midnight and flagged. The merge step in the pipeline replaces it
    with the time read from the document.
    """
    records: list[FlightRecord] = []
    for bp in passes:
        record = FlightRecord(
            carrier=bp.carrier,
            number=bp.flight_number,
            origin=Place(iata=bp.origin),
            destination=Place(iata=bp.destination),
            departure=LocalTime(local=datetime.combine(bp.flight_date, datetime.min.time())),
            seat=bp.seat,
            cabin=bp.compartment,
            confirmation=bp.pnr or None,
            traveller=bp.passenger or None,
            extraction_confidence=0.99,
            provenance=Provenance(
                extractor="barcode",
                source_file=source_file,
                note=f"IATA BCBP {bp.designator()} {bp.origin}→{bp.destination}",
            ),
        )
        record.add_issue(
            IssueLevel.INFO,
            "barcode.time_not_encoded",
            "Route, flight number, date and seat are from the boarding-pass barcode and are "
            "exact. The barcode does not encode a departure time.",
            "barcode",
        )
        records.append(record)
    return records
