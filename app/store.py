"""In-memory search store with a pub/sub feed for the dashboard (SSE).

A hackathon demo needs no persistence; restart wipes state.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from .schemas import Search


class Store:
    def __init__(self) -> None:
        self.searches: dict[str, Search] = {}
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def add(self, search: Search) -> Search:
        self.searches[search.id] = search
        return search

    def get(self, search_id: str) -> Search | None:
        return self.searches.get(search_id)

    def publish(self, search: Search) -> None:
        snapshot = search.model_dump_json()
        for q in list(self._subs[search.id]):
            q.put_nowait(snapshot)

    def subscribe(self, search_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs[search_id].add(q)
        return q

    def unsubscribe(self, search_id: str, q: asyncio.Queue) -> None:
        self._subs[search_id].discard(q)


store = Store()
