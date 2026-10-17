"""In-memory errand store with a pub/sub feed for the dashboard (SSE).

A hackathon demo needs no persistence; restart wipes state.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from .schemas import Errand


class Store:
    def __init__(self) -> None:
        self.errands: dict[str, Errand] = {}
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def add(self, errand: Errand) -> Errand:
        self.errands[errand.id] = errand
        return errand

    def get(self, errand_id: str) -> Errand | None:
        return self.errands.get(errand_id)

    def publish(self, errand: Errand) -> None:
        snapshot = errand.model_dump_json()
        for q in list(self._subs[errand.id]):
            q.put_nowait(snapshot)

    def subscribe(self, errand_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs[errand_id].add(q)
        return q

    def unsubscribe(self, errand_id: str, q: asyncio.Queue) -> None:
        self._subs[errand_id].discard(q)


store = Store()
