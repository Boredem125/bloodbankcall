"""Live calls through Bolna. Every call here spends wallet credit."""
from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..schemas import TERMINAL_STATUSES, Facility, Phone
from .base import CallStatus

# Bolna's documented auto-retry statuses (https://www.bolna.ai/docs/outbound/auto-retry).
RETRYABLE_STATUSES = ("no-answer", "busy", "failed", "error")


def retry_config(max_retries: int = 1, intervals_minutes: list[int] | None = None,
                 statuses: tuple[str, ...] = ("no-answer", "busy")) -> dict[str, Any]:
    """Build a POST /call `retry_config`. Bolna allows 1-3 retries; intervals are whole minutes."""
    if not 1 <= max_retries <= 3:
        raise ValueError("Bolna allows max_retries between 1 and 3")
    bad = set(statuses) - set(RETRYABLE_STATUSES)
    if bad:
        raise ValueError(f"not retryable on Bolna: {sorted(bad)}")
    intervals = intervals_minutes or [1] * max_retries
    if len(intervals) < max_retries or any(int(m) < 1 for m in intervals):
        raise ValueError("give one interval of at least 1 minute per retry")
    return {
        "enabled": True,
        "max_retries": max_retries,
        "retry_on_statuses": list(statuses),
        "retry_intervals_minutes": [int(m) for m in intervals[:max_retries]],
        "retry_on_voicemail": False,
        "prioritize_retries": True,
    }


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


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_execution(data: dict[str, Any]) -> CallStatus:
    tel = data.get("telephony_data") or {}
    status = data.get("status") or "unknown"
    if status == "completed" and data.get("answered_by_voice_mail"):
        status = "voicemail"  # a machine answered: we learned nothing about stock
    return CallStatus(
        status=status,
        transcript=data.get("transcript") or "",
        cost_cents=_num(data.get("total_cost")),
        duration_s=_num(data.get("conversation_duration") or tel.get("duration")),
        recording_url=tel.get("recording_url"),
        extracted=_flatten(data.get("extracted_data") or {}),
        error=data.get("error_message"),
    )


def execution_id_of(payload: dict[str, Any]) -> str | None:
    """Webhooks carry the execution id as `id` (same shape as GET /executions); some carry
    `execution_id`, and relays may wrap the record in `data`."""
    for key in ("id", "execution_id"):
        if payload.get(key):
            return str(payload[key])
    inner = payload.get("data")
    return execution_id_of(inner) if isinstance(inner, dict) else None


def merge_execution(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Fold one webhook event into what we already know about an execution.

    Events arrive per status change (queued, ringing, in-progress, call-disconnected, completed) and
    can be partial or out of order. Empty values never erase known data, and a late non-final event
    never downgrades a final status, unless it is a Bolna auto-retry (retry_count went up)."""
    merged = dict(old)
    for k, v in new.items():
        if v is None or v == "" or v == {} or v == []:
            continue
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = merge_execution(merged[k], v)
        else:
            merged[k] = v
    old_status, new_status = old.get("status"), new.get("status")
    if old_status in TERMINAL_STATUSES and new_status and new_status not in TERMINAL_STATUSES:
        if _num(new.get("retry_count")) <= _num(old.get("retry_count")):
            merged["status"] = old_status
    return merged


class BolnaProvider:
    name = "bolna"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None,
                 retry: dict[str, Any] | None = None) -> None:
        """`retry`: optional Bolna auto-retry config (see `retry_config()`). Off by default so the
        orchestrator's own fallback to the next number stays in charge."""
        self.s = settings
        self.retry = retry
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
        if self.retry:
            body["retry_config"] = self.retry
        r = await self.client.post("/call", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"bolna /call {r.status_code}: {r.text[:300]}")
        data = r.json()
        exec_id = data.get("execution_id") or data.get("id")
        if not exec_id:
            raise RuntimeError(f"bolna /call returned no execution_id: {str(data)[:300]}")
        return exec_id

    def _retry_pending(self, data: dict[str, Any]) -> bool:
        """With auto-retry on, a no-answer/busy is not final until the retries are used up."""
        if not self.retry or data.get("status") not in self.retry.get("retry_on_statuses", ()):
            return False
        return _num(data.get("retry_count")) < _num(self.retry.get("max_retries"))

    async def status(self, execution_id: str) -> CallStatus:
        cached = self.webhook_cache.get(execution_id)
        if cached and cached.get("status") in TERMINAL_STATUSES and not self._retry_pending(cached):
            return normalize_execution(cached)
        r = await self.client.get(f"/executions/{execution_id}")
        if r.status_code >= 400:
            raise RuntimeError(f"bolna /executions {r.status_code}: {r.text[:300]}")
        data = r.json()
        if cached:
            data = merge_execution(cached, data)
        st = normalize_execution(data)
        if self._retry_pending(data):
            st.status = "retrying"  # not terminal: keep polling for the retried attempt
        return st

    def ingest_webhook(self, payload: dict[str, Any]) -> str | None:
        """Store a webhook event; returns the execution id it belonged to (None if unusable)."""
        if not isinstance(payload, dict):
            return None
        if not payload.get("status") and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        exec_id = execution_id_of(payload)
        if exec_id:
            self.webhook_cache[exec_id] = merge_execution(self.webhook_cache.get(exec_id, {}), payload)
        return exec_id
