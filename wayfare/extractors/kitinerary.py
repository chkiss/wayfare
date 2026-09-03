"""KDE's KItinerary extraction engine, used when it is installed.

KItinerary carries years of hand-written parsers for real airline, rail and
hotel documents, and it is the right answer whenever the input is an original
booking PDF or email rather than a screenshot of one. It emits schema.org
JSON-LD, which this module maps onto our record types.

Entirely optional: if the binary is absent the pipeline simply relies on the
barcode and model extractors.

https://invent.kde.org/pim/kitinerary
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from ..config import get_config
from ..schema import (
    FlightRecord,
    LocalTime,
    LodgingRecord,
    Place,
    Provenance,
    Record,
    TrainRecord,
)


def available() -> bool:
    return shutil.which(get_config().kitinerary_bin) is not None


def extract(path: Path, source_file: str) -> list[Record]:
    """Run the extractor over a file and map its JSON-LD onto our records."""
    cfg = get_config()
    if not available():
        return []

    proc = subprocess.run(
        [cfg.kitinerary_bin, "--output", "json", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return []
    if not isinstance(payload, list):
        payload = [payload]

    records: list[Record] = []
    for entry in payload:
        record = _map_entry(entry, source_file)
        if record is not None:
            records.append(record)
    return records


def _provenance(source_file: str, note: str) -> Provenance:
    return Provenance(extractor="kitinerary", source_file=source_file, note=note)


def _map_entry(entry: dict, source_file: str) -> Record | None:
    kind = entry.get("@type")
    if kind == "FlightReservation":
        return _map_flight(entry, source_file)
    if kind == "TrainReservation":
        return _map_train(entry, source_file)
    if kind == "LodgingReservation":
        return _map_lodging(entry, source_file)
    return None


def _parse_time(value) -> LocalTime | None:
    """schema.org times may be a bare string or an object with a zone."""
    if isinstance(value, dict):
        text = value.get("@value")
        zone = value.get("timezone")
    else:
        text, zone = value, None
    if not isinstance(text, str) or not text:
        return None

    cleaned = text.strip()
    offset_suffix = None
    if len(cleaned) > 6 and (cleaned[-6] in "+-") and cleaned[-3] == ":":
        offset_suffix, cleaned = cleaned[-6:], cleaned[:-6]
    cleaned = cleaned.rstrip("Z")

    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            local = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return LocalTime(local=local, timezone=zone)

    try:
        local = datetime.strptime(cleaned, "%Y-%m-%d")
    except ValueError:
        return None
    return LocalTime(local=local, timezone=zone)


def _map_place(node: dict | None) -> Place:
    if not isinstance(node, dict):
        return Place()
    address = node.get("address") or {}
    street = address.get("streetAddress") if isinstance(address, dict) else None
    locality = address.get("addressLocality") if isinstance(address, dict) else None
    full = ", ".join(part for part in (street, locality) if part) or None

    geo = node.get("geo") or {}
    return Place(
        name=node.get("name"),
        iata=(node.get("iataCode") or "").strip().upper() or None,
        address=full,
        latitude=geo.get("latitude") if isinstance(geo, dict) else None,
        longitude=geo.get("longitude") if isinstance(geo, dict) else None,
    )


def _reservation_number(entry: dict) -> str | None:
    value = entry.get("reservationNumber")
    return str(value) if value else None


def _traveller(entry: dict) -> str | None:
    person = entry.get("underName")
    if isinstance(person, dict):
        return person.get("name")
    return None


def _map_flight(entry: dict, source_file: str) -> FlightRecord | None:
    flight = entry.get("reservationFor") or {}
    departure = _parse_time(flight.get("departureTime"))
    if departure is None:
        return None

    airline = flight.get("airline") or {}
    ticket = entry.get("reservedTicket") or {}
    seat = ticket.get("ticketedSeat") or {}

    return FlightRecord(
        carrier=(airline.get("iataCode") or "").strip().upper() or None,
        number=str(flight.get("flightNumber") or "").strip() or None,
        origin=_map_place(flight.get("departureAirport")),
        destination=_map_place(flight.get("arrivalAirport")),
        departure=departure,
        arrival=_parse_time(flight.get("arrivalTime")),
        seat=seat.get("seatNumber") if isinstance(seat, dict) else None,
        cabin=seat.get("seatingType") if isinstance(seat, dict) else None,
        terminal_departure=flight.get("departureTerminal"),
        terminal_arrival=flight.get("arrivalTerminal"),
        confirmation=_reservation_number(entry),
        traveller=_traveller(entry),
        extraction_confidence=0.92,
        provenance=_provenance(source_file, "KItinerary FlightReservation"),
    )


def _map_train(entry: dict, source_file: str) -> TrainRecord | None:
    trip = entry.get("reservationFor") or {}
    departure = _parse_time(trip.get("departureTime"))
    if departure is None:
        return None

    operator = trip.get("provider") or {}
    ticket = entry.get("reservedTicket") or {}
    seat = ticket.get("ticketedSeat") or {}

    return TrainRecord(
        operator=operator.get("name") if isinstance(operator, dict) else None,
        number=str(trip.get("trainNumber") or "").strip() or None,
        origin=_map_place(trip.get("departureStation")),
        destination=_map_place(trip.get("arrivalStation")),
        departure=departure,
        arrival=_parse_time(trip.get("arrivalTime")),
        coach=seat.get("seatSection") if isinstance(seat, dict) else None,
        seat=seat.get("seatNumber") if isinstance(seat, dict) else None,
        confirmation=_reservation_number(entry),
        traveller=_traveller(entry),
        extraction_confidence=0.92,
        provenance=_provenance(source_file, "KItinerary TrainReservation"),
    )


def _map_lodging(entry: dict, source_file: str) -> LodgingRecord | None:
    check_in = _parse_time(entry.get("checkinTime"))
    check_out = _parse_time(entry.get("checkoutTime"))
    if check_in is None or check_out is None:
        return None

    hotel = entry.get("reservationFor") or {}
    return LodgingRecord(
        property_name=hotel.get("name") if isinstance(hotel, dict) else None,
        location=_map_place(hotel),
        check_in=check_in,
        check_out=check_out,
        confirmation=_reservation_number(entry),
        traveller=_traveller(entry),
        extraction_confidence=0.92,
        provenance=_provenance(source_file, "KItinerary LodgingReservation"),
    )
