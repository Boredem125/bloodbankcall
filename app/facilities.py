"""Blood banks and hospitals to call, plus travel-time estimates."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .schemas import Facility

# Rough centres of Bengaluru areas (demo geography; swap for a geocoder later).
AREAS: dict[str, tuple[float, float]] = {
    "HSR Layout": (12.9116, 77.6474),
    "Koramangala": (12.9352, 77.6245),
    "BTM Layout": (12.9166, 77.6101),
    "Jayanagar": (12.9250, 77.5938),
    "Indiranagar": (12.9719, 77.6412),
    "Whitefield": (12.9698, 77.7500),
    "Bellandur": (12.9257, 77.6764),
    "Malleshwaram": (13.0031, 77.5643),
    "Electronic City": (12.8452, 77.6602),
    "Hebbal": (13.0358, 77.5970),
}
CITY_KMPH = 22  # average Bengaluru road speed, generous at night


def load_facilities(path: Path) -> list[Facility]:
    return [Facility(**f) for f in json.loads(path.read_text(encoding="utf-8"))]


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h)) * 1.35  # road distance ~ 1.35x straight line


def eta_minutes(area: str, f: Facility) -> int:
    origin = AREAS.get(area, AREAS["HSR Layout"])
    return max(5, round(_km(origin, (f.lat, f.lon)) / CITY_KMPH * 60))


def call_order(facilities: list[Facility], area: str) -> list[Facility]:
    """24x7 first (they answer at night), then nearest."""
    return sorted(facilities, key=lambda f: (not f.open_24x7, eta_minutes(area, f)))
