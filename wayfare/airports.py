"""Offline airport reference data.

Backed by the OurAirports database (public domain), which gives every IATA
code a name, a country and coordinates. Timezones are derived from those
coordinates with `timezonefinder`, then cached.

The point of doing this offline is that the two checks that catch the most
damaging parse errors — wrong timezone and impossible block time — must work
with no API key, no network and no rate limit, forever.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import get_config

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

#: Airport types worth matching. Excludes closed fields and heliports, which
#: reuse codes and would otherwise produce phantom matches.
USEFUL_TYPES = {"large_airport", "medium_airport", "small_airport"}


#: Bigger airports win a name clash. "Paris" must not resolve to a Tennessee
#: airfield, and it did.
SIZE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    municipality: str
    country: str
    latitude: float
    longitude: float
    kind: str = "small_airport"

    @property
    def rank(self) -> int:
        return SIZE_RANK.get(self.kind, 3)

    @property
    def city(self) -> str:
        """The city, without the administrative detail OurAirports appends.

        CDG is recorded as "Paris (Roissy-en-France, Val-d'Oise)", which is
        accurate and useless in a calendar entry.
        """
        name = self.municipality.split("(")[0]
        return name.split(",")[0].strip()

    def label(self) -> str:
        return self.city or self.name


class AirportDB:
    """Lazily loaded IATA lookup table."""

    def __init__(self, csv_path: Path | None = None) -> None:
        cfg = get_config()
        self.csv_path = csv_path or cfg.airports_csv
        self._tz_cache_path = self.csv_path.parent / "airport_tz.cache"
        self._by_iata: dict[str, Airport] | None = None
        self._by_city: dict[str, Airport] | None = None
        self._tz_cache: dict[str, str] | None = None
        self._finder = None

    # --- loading ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.csv_path.exists()

    def _load(self) -> dict[str, Airport]:
        if self._by_iata is not None:
            return self._by_iata
        table: dict[str, Airport] = {}
        if not self.csv_path.exists():
            self._by_iata = table
            return table
        with self.csv_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                iata = (row.get("iata_code") or "").strip().upper()
                if len(iata) != 3 or row.get("type") not in USEFUL_TYPES:
                    continue
                try:
                    lat = float(row["latitude_deg"])
                    lon = float(row["longitude_deg"])
                except (KeyError, TypeError, ValueError):
                    continue
                table[iata] = Airport(
                    iata=iata,
                    name=(row.get("name") or "").strip(),
                    municipality=(row.get("municipality") or "").strip(),
                    country=(row.get("iso_country") or "").strip(),
                    latitude=lat,
                    longitude=lon,
                    kind=row.get("type") or "small_airport",
                )
        self._by_iata = table
        return table

    def get(self, iata: str | None) -> Airport | None:
        if not iata:
            return None
        return self._load().get(iata.strip().upper())

    def __len__(self) -> int:
        return len(self._load())

    # --- timezones -------------------------------------------------------
    def _load_tz_cache(self) -> dict[str, str]:
        if self._tz_cache is not None:
            return self._tz_cache
        try:
            self._tz_cache = json.loads(self._tz_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._tz_cache = {}
        return self._tz_cache

    def _save_tz_cache(self) -> None:
        if self._tz_cache is None:
            return
        try:
            self._tz_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._tz_cache_path.write_text(json.dumps(self._tz_cache), encoding="utf-8")
        except OSError:
            pass  # A cold cache is slow, not wrong.

    def timezone_for(self, iata: str | None) -> str | None:
        """IANA zone for an airport, or None if it cannot be determined."""
        airport = self.get(iata)
        if airport is None:
            return None
        cache = self._load_tz_cache()
        if airport.iata in cache:
            return cache[airport.iata] or None
        zone = self.timezone_at(airport.latitude, airport.longitude)
        cache[airport.iata] = zone or ""
        self._save_tz_cache()
        return zone

    def timezone_at(self, latitude: float, longitude: float) -> str | None:
        """IANA zone for a coordinate pair, using the offline boundary data."""
        if self._finder is None:
            try:
                from timezonefinder import TimezoneFinder
            except ImportError:
                return None
            self._finder = TimezoneFinder()
        try:
            return self._finder.timezone_at(lat=latitude, lng=longitude)
        except Exception:
            return None


    # --- place lookup ----------------------------------------------------
    def _load_cities(self) -> dict[str, Airport]:
        """Municipality name to its largest airport, for non-airport places.

        A hotel booking gives a city and a street, never an IATA code, so its
        times would otherwise have no zone at all. Matching the city against
        the airport table is a cheap offline way to get one: somewhere with a
        hotel almost always has an airport named after it.
        """
        if self._by_city is not None:
            return self._by_city

        table: dict[str, Airport] = {}
        for airport in self._load().values():
            for name in {airport.city, airport.municipality}:
                key = name.strip().casefold()
                if len(key) < 3:
                    continue
                held = table.get(key)
                if held is None or airport.rank < held.rank:
                    table[key] = airport
        self._by_city = table
        return table

    def find_place(self, *candidates: str | None) -> Airport | None:
        """The airport serving a named place, tried in order of preference.

        Only used to derive a timezone for somewhere that has no IATA code, so
        the nearest large airport is a good enough answer — precision beyond
        the timezone would be wasted.
        """
        cities = self._load_cities()

        parts: list[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            text = candidate.strip().casefold()
            parts.append(text)
            # "Brussels, Belgium" or "12 High St, London" — try the pieces too,
            # rightmost first, since an address ends with its city or country.
            parts.extend(p.strip() for p in reversed(text.split(",")))

        for part in parts:
            if len(part) >= 3 and part in cities:
                return cities[part]

        # Many airports are not named after the city they serve: Brussels is
        # recorded at Zaventem, Frankfurt as "Frankfurt am Main". Fall back to
        # the airport's own name, biggest first.
        for part in parts:
            if len(part) < 4:
                continue
            match = self._search_names(part)
            if match:
                return match
        return None

    def _search_names(self, needle: str) -> Airport | None:
        best: Airport | None = None
        for airport in self._load().values():
            if airport.rank > 1:  # Large and medium airports only.
                continue
            haystack = f"{airport.name} {airport.municipality}".casefold()
            if needle in haystack and (best is None or airport.rank < best.rank):
                best = airport
                if best.rank == 0:
                    break
        return best


@lru_cache(maxsize=1)
def get_airport_db() -> AirportDB:
    return AirportDB()


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in kilometres between two points on the earth."""
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def distance_between(origin_iata: str, destination_iata: str) -> float | None:
    db = get_airport_db()
    a, b = db.get(origin_iata), db.get(destination_iata)
    if a is None or b is None:
        return None
    return great_circle_km(a.latitude, a.longitude, b.latitude, b.longitude)
