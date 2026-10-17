from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from .base import Answers, DecisionError, Questions


class JevEngine:
    """TypeSafe AI Jev: POST https://api.typesafe.ai/v1/systemone"""

    name = "jev"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.client = client or httpx.AsyncClient(timeout=10)

    async def decide(self, state: Any, questions: Questions) -> Answers:
        r = await self.client.post(
            self.s.typesafe_url,
            headers={"Authorization": f"Bearer {self.s.typesafe_api_key}"},
            json={"model": self.s.jev_model, "state": state, "questions": questions},
        )
        if r.status_code >= 400:
            raise DecisionError(f"jev {r.status_code}: {r.text[:200]}")
        return r.json()["answers"]


class ClefEngine:
    """Cloudflare Clef on Workers AI. API-compatible with Jev; Cloudflare wraps the body in `result`."""

    name = "clef"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.client = client or httpx.AsyncClient(timeout=15)

    @property
    def url(self) -> str:
        return (
            f"https://api.cloudflare.com/client/v4/accounts/{self.s.cf_account_id}"
            f"/ai/run/@cf/cloudflare/{self.s.clef_model}"
        )

    async def decide(self, state: Any, questions: Questions) -> Answers:
        r = await self.client.post(
            self.url,
            headers={"Authorization": f"Bearer {self.s.cf_api_token}"},
            json={"model": self.s.clef_model, "state": state, "questions": questions},
        )
        if r.status_code >= 400:
            raise DecisionError(f"clef {r.status_code}: {r.text[:200]}")
        body = r.json()
        return (body.get("result") or body)["answers"]
