"""Live calls through Bolna. Every call here spends wallet credit."""
from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..schemas import Facility, Phone
from .base import CallStatus


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    """Bolna extractions arrive nested by category; flatten to leaf keys."""
    out: dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict) and not ({"subjective", "objective"} & set(v)):
                out.update(_flatten(v, k))
            elif isinstance(v, dict):
                out[k] = v.get("objective") or v.get("subjective")
            else:
                out[k] = v
    return out


def normalize_execution(data: dict[str, Any]) -> CallStatus:
    tel = data.get("telephony_data") or {}
    status = data.get("status") or "unknown"
    if status == "completed" and data.get("answered_by_voice_mail"):
        status = "voicemail"  # a machine answered: we learned nothing about stock
    return CallStatus(
        status=status,
        transcript=data.get("transcript") or "",
        cost_cents=float(data.get("total_cost") or 0),
        duration_s=float(data.get("conversation_duration") or 0),
        recording_url=tel.get("recording_url"),
        extracted=_flatten(data.get("extracted_data") or {}),
        error=data.get("error_message"),
    )


class BolnaProvider:
    name = "bolna"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.client = client or httpx.AsyncClient(
            base_url=settings.bolna_base_url,
            headers={"Authorization": f"Bearer {settings.bolna_api_key}"},
            timeout=20,
        )
        # Filled by /webhooks/bolna when a tunnel is configured; saves polling calls.
        self.webhook_cache: dict[str, dict[str, Any]] = {}

    async def start(self, facility: Facility, phone: Phone, user_data: dict[str, Any]) -> str:
        body: dict[str, Any] = {
            "agent_id": self.s.bolna_agent_id,
            "recipient_phone_number": phone.number,
            "user_data": user_data,
        }
        if self.s.bolna_from_number:
            body["from_phone_number"] = self.s.bolna_from_number
        r = await self.client.post("/call", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"bolna /call {r.status_code}: {r.text[:300]}")
        return r.json()["execution_id"]

    async def status(self, execution_id: str) -> CallStatus:
        cached = self.webhook_cache.get(execution_id)
        if cached and cached.get("status") == "completed":
            return normalize_execution(cached)
        r = await self.client.get(f"/executions/{execution_id}")
        if r.status_code >= 400:
            raise RuntimeError(f"bolna /executions {r.status_code}: {r.text[:300]}")
        return normalize_execution(r.json())

    def ingest_webhook(self, payload: dict[str, Any]) -> None:
        exec_id = payload.get("id") or payload.get("execution_id")
        if exec_id:
            self.webhook_cache[exec_id] = payload
