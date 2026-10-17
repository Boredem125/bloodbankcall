from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

Priority = Literal["cheapest", "fastest", "reliable"]

CATEGORIES: dict[str, str] = {
    "ac_repair": "AC repair, servicing or gas refill",
    "plumber": "Plumbing: leaks, taps, pipes, drainage",
    "electrician": "Electrical work: wiring, switches, fans, inverter",
    "pg_rental": "PG, hostel or room rental",
    "caterer": "Catering or food for an event",
    "movers": "Packers and movers, shifting a home",
}

TERMINAL_STATUSES = {
    "completed", "no-answer", "busy", "failed", "canceled", "stopped", "error", "balance-low",
}


def _id() -> str:
    return uuid.uuid4().hex[:10]


class ErrandRequest(BaseModel):
    task: str = Field(..., min_length=3, description="Free text, e.g. 'AC service, cooling is weak'")
    area: str = "HSR Layout, Bengaluru"
    time_window: str = "tomorrow evening"
    budget: int = Field(800, gt=0, description="Max total price in rupees")
    priority: Priority = "cheapest"
    language: str = "Hindi or English"
    auto_book_limit: int = Field(0, ge=0, description="Auto-book without asking if price <= this; 0 disables")


class Vendor(BaseModel):
    id: str
    name: str
    category: str
    area: str
    rating: float
    phone: str
    sim: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    """One call to the decision model, kept for the audit trail."""

    stage: str  # intake | call | final
    backend: str
    latency_ms: float
    answers: dict[str, Any]
    subject: str | None = None  # call id or None


class CallRecord(BaseModel):
    id: str = Field(default_factory=_id)
    vendor: Vendor
    purpose: Literal["quote", "book"] = "quote"
    execution_id: str | None = None
    status: str = "pending"
    transcript: str = ""
    price: int | None = None
    slot: str | None = None
    extras: str | None = None
    signals: dict[str, float] = Field(default_factory=dict)
    outcome: str | None = None
    rank_score: float | None = None
    viable: bool = False
    cost_cents: float = 0.0
    duration_s: float = 0.0
    recording_url: str | None = None
    error: str | None = None


class Errand(BaseModel):
    id: str = Field(default_factory=_id)
    created_at: float = Field(default_factory=time.time)
    request: ErrandRequest
    category: str | None = None
    status: str = "planning"  # planning | calling | deciding | awaiting_approval | booking | booked | failed
    calls: list[CallRecord] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    chosen_call_id: str | None = None
    choice_confidence: float | None = None
    auto_book_probability: float | None = None
    reason: str | None = None
    log: list[str] = Field(default_factory=list)

    def call(self, call_id: str) -> CallRecord:
        return next(c for c in self.calls if c.id == call_id)

    def note(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
