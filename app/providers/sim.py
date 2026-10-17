"""Zero-cost call simulator. Replays persona scripts with realistic timing.

Each vendor in data/vendors.json carries a `sim` block: persona, price, slot, extra,
ring_s, talk_s. Transcript lines appear progressively while the call is "in progress",
so the dashboard looks exactly like it will with live Bolna calls.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..schemas import Vendor
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
    final: str  # completed | no-answer | busy


class SimProvider:
    name = "sim"

    def __init__(self, settings: Settings, clock=time.monotonic) -> None:
        self.speed = max(settings.sim_speed, 0.01)
        self.scripts: dict[str, Any] = json.loads(settings.sim_scripts_file.read_text(encoding="utf-8"))
        self.clock = clock
        self.calls: dict[str, _SimCall] = {}

    async def start(self, vendor: Vendor, user_data: dict[str, Any]) -> str:
        sim = {"persona": "good_value", "price": 700, "slot": "6 pm", "extra": "", "ring_s": 3, "talk_s": 14}
        sim.update(vendor.sim)
        persona = sim["persona"] if user_data.get("mode") != "book" else "booking"
        script = self.scripts[persona]
        fill = _Blank({**sim, **user_data, "business": vendor.name})
        lines = [ln.format_map(fill) for ln in script.get("lines", [])]
        exec_id = f"sim-{uuid.uuid4().hex[:12]}"
        self.calls[exec_id] = _SimCall(
            started=self.clock(),
            lines=lines,
            ring_s=float(sim["ring_s"]),
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
        return CallStatus(
            status="completed",
            transcript="\n".join(c.lines),
            duration_s=c.talk_s,
            recording_url=None,
        )
