from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..schemas import Facility, Phone


class CallStatus(BaseModel):
    status: str
    transcript: str = ""
    cost_cents: float = 0.0
    duration_s: float = 0.0
    recording_url: str | None = None
    extracted: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class CallProvider(Protocol):
    name: str

    async def start(self, facility: Facility, phone: Phone, user_data: dict[str, Any],
                    agent_id: str | None = None) -> str:
        """Place a call; returns an execution id. `agent_id` overrides the default (caller) agent."""
        ...

    async def status(self, execution_id: str) -> CallStatus: ...
