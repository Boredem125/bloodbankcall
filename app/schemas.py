from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

BloodGroup = Literal["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
BLOOD_GROUPS: list[str] = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]

# How the agent says each group on the phone: name plus an unmistakable second form.
GROUP_SPOKEN: dict[str, str] = {
    g: f"{g[:-1]} {'positive' if g.endswith('+') else 'negative'}" for g in BLOOD_GROUPS
}
GROUP_CONFIRM: dict[str, str] = {
    g: f"{g[:-1]} {'plus' if g.endswith('+') else 'minus'}" for g in BLOOD_GROUPS
}

COMPONENTS: dict[str, str] = {
    "prbc": "packed red cells (PRBC)",
    "whole_blood": "whole blood",
    "platelets": "platelets",
    "ffp": "fresh frozen plasma",
}

# Live call states that end a call.
TERMINAL_STATUSES = {
    "completed", "no-answer", "busy", "failed", "canceled", "stopped", "error", "balance-low",
}
NOT_REACHED = {"no-answer", "busy", "failed", "canceled", "stopped", "error", "timeout", "voicemail"}

# What we tell the family about a facility. Only "confirmed" is ever presented as available.
Availability = Literal["confirmed", "unconfirmed", "not_available", "closed", "unknown"]


def _id() -> str:
    return uuid.uuid4().hex[:10]


class BloodRequest(BaseModel):
    blood_group: BloodGroup = "O-"
    component: Literal["prbc", "whole_blood", "platelets", "ffp"] = "prbc"
    units: int = Field(2, ge=1, le=10)
    area: str = "HSR Layout"  # where the patient is; used for travel time
    hospital: str = Field("", max_length=120, description="Patient's hospital (no patient name: privacy)")
    language: str = "English or Hindi"


class Phone(BaseModel):
    number: str
    label: str = "blood bank"  # blood bank | switchboard | casualty


class Facility(BaseModel):
    id: str
    name: str
    area: str
    lat: float
    lon: float
    open_24x7: bool = False
    phones: list[Phone]
    sim: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    """One call to the decision model, kept for the audit trail."""

    stage: str  # call | final
    backend: str
    latency_ms: float
    answers: dict[str, Any]
    subject: str | None = None


class CallRecord(BaseModel):
    id: str = Field(default_factory=_id)
    facility: Facility
    phone: Phone
    attempt: int = 1  # 2+ = fallback number for the same facility
    execution_id: str | None = None
    status: str = "pending"
    transcript: str = ""
    # parsed from the transcript
    units: int | None = None
    fee: int | None = None
    staff: str | None = None
    groups_heard: list[str] = Field(default_factory=list)
    # decided
    signals: dict[str, float] = Field(default_factory=dict)
    outcome: str | None = None
    availability: Availability = "unknown"
    gate_reasons: list[str] = Field(default_factory=list)
    rank_score: float | None = None
    eta_min: int | None = None
    confirmed_at: float | None = None
    reused_at: float | None = None  # set when this answer came from the freshness cache (when it was heard)
    cost_cents: float = 0.0
    duration_s: float = 0.0
    recording_url: str | None = None
    error: str | None = None


class PlanItem(BaseModel):
    call_id: str
    facility: str
    units: int


class Search(BaseModel):
    id: str = Field(default_factory=_id)
    created_at: float = Field(default_factory=time.time)
    request: BloodRequest
    status: str = "calling"  # calling | deciding | done | failed
    calls: list[CallRecord] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    chosen_call_id: str | None = None
    choice_confidence: float | None = None
    plan: list[PlanItem] = Field(default_factory=list)  # may combine facilities for enough units
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    log: list[str] = Field(default_factory=list)

    def call(self, call_id: str) -> CallRecord:
        return next(c for c in self.calls if c.id == call_id)

    def note(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
