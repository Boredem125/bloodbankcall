"""Zero-cost call simulator. Replays persona scripts with realistic timing.

A facility's `sim` block sets a persona per phone number (`personas`, in the same
order as `phones`) or one `persona` for all, plus script values (units, fee, staff...).
Transcript lines appear progressively while the call is "in progress", exactly like
live Bolna calls on the dashboard.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..schemas import Facility, Phone
from .base import CallStatus


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


@dataclass
class _SimCall:
    started: float
    lines: list[str]
    ring_s: float
    talk_s: float
    final: str  # completed | no-answer | busy | voicemail


class SimProvider:
    name = "sim"

    def __init__(self, settings: Settings, clock=time.monotonic) -> None:
        self.speed = max(settings.sim_speed, 0.01)
        self.scripts: dict[str, Any] = json.loads(settings.sim_scripts_file.read_text(encoding="utf-8"))
        self.clock = clock
        self.calls: dict[str, _SimCall] = {}

    def persona(self, facility: Facility, phone: Phone) -> str:
        sim = facility.sim
        idx = next((i for i, p in enumerate(facility.phones) if p.number == phone.number), 0)
        personas = sim.get("personas") or [sim.get("persona", "has_stock")]
        return personas[min(idx, len(personas) - 1)]

    async def start(self, facility: Facility, phone: Phone, user_data: dict[str, Any]) -> str:
        sim = {"units": 3, "fee": 1550, "staff": "Ramesh", "ring_s": 3, "talk_s": 18, **facility.sim}
        script = self.scripts[self.persona(facility, phone)]
        fill = _Blank({**sim, **user_data})
        lines = [ln.format_map(fill) for ln in script.get("lines", [])]
        exec_id = f"sim-{uuid.uuid4().hex[:12]}"
        self.calls[exec_id] = _SimCall(
            started=self.clock(),
            lines=lines,
            ring_s=float(script.get("ring_s", sim["ring_s"])),
            talk_s=float(sim["talk_s"]) if lines else 0.0,
            final=script.get("final", "completed"),
        )
        return exec_id

    async def status(self, execution_id: str) -> CallStatus:
        c = self.calls[execution_id]
        t = (self.clock() - c.started) * self.speed
        if t < 1:
            return CallStatus(status="queued")
        if t < 1 + c.ring_s:
            return CallStatus(status="ringing")
        if c.final != "completed":
            return CallStatus(status=c.final)
        talk_t = t - 1 - c.ring_s
        if talk_t < c.talk_s:
            shown = max(1, int(len(c.lines) * talk_t / c.talk_s))
            return CallStatus(status="in-progress", transcript="\n".join(c.lines[:shown]))
        return CallStatus(status="completed", transcript="\n".join(c.lines), duration_s=c.talk_s)
