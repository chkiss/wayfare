"""Test fixtures.

The validators need real airport coordinates to do anything interesting, so
the suite ships a tiny airport table rather than depending on the full
OurAirports download being present. Coordinates are the real ones — the
distance checks are only meaningful if they are.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_HEADER = (
    "id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,"
    "iso_region,municipality,scheduled_service,gps_code,iata_code,local_code,home_link,"
    "wikipedia_link,keywords"
)

_ROWS = [
    ("EGLL", "London Heathrow Airport", 51.4706, -0.461941, "GB", "London", "LHR"),
    ("KJFK", "John F Kennedy International Airport", 40.639447, -73.779317, "US", "New York", "JFK"),
    ("KORD", "Chicago O'Hare International Airport", 41.978603, -87.904842, "US", "Chicago", "ORD"),
    ("EDDF", "Frankfurt am Main Airport", 50.033306, 8.570456, "DE", "Frankfurt am Main", "FRA"),
    ("CYUL", "Montreal / Pierre Elliott Trudeau International", 45.470556, -73.740833, "CA", "Montréal", "YUL"),
    ("EBBR", "Brussels Airport", 50.901402, 4.48444, "BE", "Brussels", "BRU"),
]


def _write_airports(path: Path) -> None:
    lines = [_HEADER]
    for index, (ident, name, lat, lon, country, city, iata) in enumerate(_ROWS, start=1):
        lines.append(
            f"{index},{ident},large_airport,\"{name}\",{lat},{lon},0,EU,{country},"
            f"{country}-X,{city},yes,{ident},{iata},,,,"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Point the whole package at a throwaway data and state directory before any
# wayfare module reads its configuration.
_TMPDIR = tempfile.TemporaryDirectory(prefix="wayfare-tests-")
_ROOT = Path(_TMPDIR.name)
(_ROOT / "data").mkdir()
_write_airports(_ROOT / "data" / "airports.csv")

os.environ["WAYFARE_DATA_DIR"] = str(_ROOT / "data")
os.environ["WAYFARE_STATE_DIR"] = str(_ROOT / "var")
os.environ["WAYFARE_SECRETS_DIR"] = str(_ROOT / "secrets")
os.environ.pop("WAYFARE_CONVENTIONS", None)
