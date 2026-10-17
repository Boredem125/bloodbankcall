"""Blood banks and hospitals to call, plus travel-time estimates."""
from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from pathlib import Path

from .schemas import Facility, Phone

log = logging.getLogger(__name__)

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


class PickupStats:
    """Answered / not answered per phone number and hour of day, learned from our own calls.

    Pickup chance is smoothed toward 50% (a prior worth a few calls), so one missed call
    does not bury a number. Optionally persisted to a small JSON file.
    """

    PRIOR_CALLS = 4

    def __init__(self, path: Path | None = None, clock=time.time) -> None:
        self.path = path
        self.clock = clock
        # number -> hour -> [answered, total]
        self._counts: dict[str, dict[int, list[int]]] = defaultdict(dict)
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for number, hours in raw.items():
                    self._counts[number] = {int(h): [int(a), int(n)] for h, (a, n) in hours.items()}
            except (ValueError, TypeError) as exc:
                log.warning("ignoring unreadable pickup stats %s: %s", path, exc)

    def hour(self) -> int:
        return time.localtime(self.clock()).tm_hour

    def record(self, number: str, answered: bool, hour: int | None = None) -> None:
        h = self.hour() if hour is None else hour
        a, n = self._counts[number].get(h, [0, 0])
        self._counts[number][h] = [a + int(answered), n + 1]
        if self.path:
            try:
                self.path.write_text(json.dumps(self._counts), encoding="utf-8")
            except OSError as exc:
                log.warning("could not save pickup stats: %s", exc)

    def chance(self, number: str, hour: int | None = None) -> float:
        h = self.hour() if hour is None else hour
        a, n = self._counts.get(number, {}).get(h, [0, 0])
        return (a + self.PRIOR_CALLS / 2) / (n + self.PRIOR_CALLS)

    def facility_chance(self, f: Facility, hour: int | None = None) -> float:
        """Chance that at least one of the facility's numbers answers."""
        miss = 1.0
        for p in f.phones:
            miss *= 1 - self.chance(p.number, hour)
        return 1 - miss


PHONE_MARGIN = 0.2  # a fallback number must clearly out-answer the listed first one to jump ahead


def phone_order(f: Facility, stats: PickupStats | None = None, hour: int | None = None) -> list[Phone]:
    """Listed order (direct blood bank line first) unless another number clearly answers better now."""
    phones = list(f.phones)
    if stats is None or len(phones) < 2:
        return phones
    best = max(phones, key=lambda p: stats.chance(p.number, hour))
    if stats.chance(best.number, hour) - stats.chance(phones[0].number, hour) >= PHONE_MARGIN:
        phones.remove(best)
        phones.insert(0, best)
    return phones


def call_order(facilities: list[Facility], area: str, stats: PickupStats | None = None,
               hour: int | None = None) -> list[Facility]:
    """24x7 first (they answer at night), then by travel time stretched by how often they
    fail to pick up at this hour (no history = plain nearest first). The stretch is squared:
    a live call slot spent on a line that rarely answers is the scarcest thing we have."""
    if stats is None:
        return sorted(facilities, key=lambda f: (not f.open_24x7, eta_minutes(area, f)))
    h = stats.hour() if hour is None else hour

    def key(f: Facility):
        unknown = 1 - 0.5 ** len(f.phones)  # chance with no history, so no history = plain eta
        chance = max(stats.facility_chance(f, h), 0.05)
        return (not f.open_24x7, eta_minutes(area, f) * (unknown / chance) ** 2)

    return sorted(facilities, key=key)
