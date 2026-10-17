"""Freshness cache: recent definite answers from a blood bank, reused instead of calling again.

Only "confirmed" and "not_available" answers from a trusted decision backend are kept, keyed
by facility + blood group + component. Entries older than the TTL are never reused: stock
moves fast, so an old "yes" must be re-called.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .schemas import BloodRequest, CallRecord, _id

CACHEABLE = {"confirmed", "not_available"}


@dataclass
class _Entry:
    call: CallRecord  # deep copy of the call as it was assessed
    heard_at: float  # wall clock when the answer was heard
    req_units: int  # units asked for at the time (enough_units depended on it)


class FreshnessCache:
    def __init__(self, ttl_s: float, clock=time.time) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self._entries: dict[tuple[str, str, str], _Entry] = {}

    @staticmethod
    def _key(facility_id: str, req: BloodRequest) -> tuple[str, str, str]:
        return facility_id, req.blood_group, req.component

    def put(self, call: CallRecord, req: BloodRequest) -> None:
        if call.availability not in CACHEABLE or call.reused_at is not None:
            return
        self._entries[self._key(call.facility.id, req)] = _Entry(call.model_copy(deep=True), self.clock(), req.units)

    def get(self, facility_id: str, req: BloodRequest) -> tuple[CallRecord, float] | None:
        """A fresh copy of the cached call (new id, reused_at set) and its age in seconds."""
        key = self._key(facility_id, req)
        e = self._entries.get(key)
        if e is None:
            return None
        age = self.clock() - e.heard_at
        if age > self.ttl_s or age < 0:
            del self._entries[key]
            return None
        # A "yes" heard for fewer units says nothing about a bigger request unless the count was stated.
        if e.call.availability == "confirmed" and req.units > e.req_units and e.call.units is None:
            return None
        call = e.call.model_copy(deep=True, update={"id": _id(), "reused_at": e.heard_at})
        if call.units is not None and call.availability == "confirmed":
            call.signals["enough_units"] = 1.0 if call.units >= req.units else 0.0
        return call, age


def age_text(age_s: float) -> str:
    m = int(age_s // 60)
    return "under a minute ago" if m < 1 else f"{m} min ago"
