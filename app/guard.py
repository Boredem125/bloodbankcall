"""Abuse and load guards.

SearchGuard: per-client searches per hour plus a global cap on searches running at once.
NumberLimiter: never ring the same phone number more than K times an hour, across all searches.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

HOUR_S = 3600.0


class _Window:
    """Timestamps of events in the last hour, per key."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def recent(self, key: str) -> deque[float]:
        q, cutoff = self._events[key], self.clock() - HOUR_S
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def add(self, key: str) -> None:
        self._events[key].append(self.clock())


class SearchGuard:
    def __init__(self, max_per_hour: int, max_concurrent: int, clock=time.time) -> None:
        self.max_per_hour = max_per_hour
        self.max_concurrent = max_concurrent
        self.active = 0
        self._window = _Window(clock)

    def admit(self, client: str) -> str | None:
        """Counts the search and returns None if allowed, else a message for the 429."""
        recent = self._window.recent(client)
        if len(recent) >= self.max_per_hour:
            wait = max(1, round((recent[0] + HOUR_S - self._window.clock()) / 60))
            return (
                f"Too many searches from this connection ({self.max_per_hour} per hour). "
                f"Try again in about {wait} min. In an emergency, call the blood banks or 104 directly."
            )
        if self.active >= self.max_concurrent:
            return (
                "Too many searches are running right now. Try again in a minute. "
                "In an emergency, call the blood banks or 104 directly."
            )
        self._window.add(client)
        self.active += 1
        return None

    def release(self) -> None:
        self.active = max(0, self.active - 1)


def client_key(request, trust_forwarded_for: bool = False) -> str:
    """Request IP. X-Forwarded-For is only trusted behind a known proxy (it is trivially spoofed)."""
    if trust_forwarded_for:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd.strip():
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_guard(state) -> SearchGuard:
    """One guard per app run: rebuilt whenever the lifespan builds a new orchestrator."""
    guard = getattr(state, "guard", None)
    if guard is None or getattr(state, "guard_owner", None) is not state.orch:
        s = state.settings
        guard = SearchGuard(s.max_searches_per_client_per_hour, s.max_concurrent_searches)
        state.guard, state.guard_owner = guard, state.orch
    return guard


class NumberLimiter:
    def __init__(self, max_per_hour: int, clock=time.time) -> None:
        self.max_per_hour = max_per_hour
        self._window = _Window(clock)

    @property
    def clock(self):
        return self._window.clock

    @clock.setter
    def clock(self, fn) -> None:
        self._window.clock = fn

    def allow(self, number: str) -> bool:
        return len(self._window.recent(number)) < self.max_per_hour

    def record(self, number: str) -> None:
        self._window.add(number)
