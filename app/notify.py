"""SMS to the family after a phone-intake search. Optional: `SMS_PROVIDER=none` only logs.

Plivo and Twilio both take a basic-auth POST; nothing else is needed, so no SDKs.
"""
from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)


def sms_request(settings: Settings, to: str, text: str) -> tuple[str, dict, dict] | None:
    """(url, auth-free json or form body, kwargs for httpx) for the configured provider; None if off."""
    s = settings
    if s.sms_provider == "plivo":
        return (
            f"https://api.plivo.com/v1/Account/{s.sms_auth_id}/Message/",
            {"json": {"src": s.sms_from, "dst": to.lstrip("+"), "text": text}},
            {"auth": (s.sms_auth_id, s.sms_auth_token)},
        )
    if s.sms_provider == "twilio":
        return (
            f"https://api.twilio.com/2010-04-01/Accounts/{s.sms_auth_id}/Messages.json",
            {"data": {"From": s.sms_from, "To": to, "Body": text}},
            {"auth": (s.sms_auth_id, s.sms_auth_token)},
        )
    if s.sms_provider not in ("", "none"):
        raise ValueError(f"SMS_PROVIDER must be none, plivo or twilio, not {s.sms_provider!r}")
    return None


async def send_sms(settings: Settings, to: str, text: str, client: httpx.AsyncClient | None = None) -> str:
    """Returns a short status for the dashboard: 'sent', 'skipped (...)' or 'failed: ...'."""
    req = sms_request(settings, to, text)
    if req is None:
        log.info("SMS to %s skipped (SMS_PROVIDER=none):\n%s", to, text)
        return "skipped (no SMS provider configured)"
    if not (settings.sms_auth_id and settings.sms_auth_token and settings.sms_from):
        return "skipped (SMS_AUTH_ID, SMS_AUTH_TOKEN and SMS_FROM are needed)"
    url, body, kw = req
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        r = await client.post(url, **body, **kw)
        if r.status_code >= 400:
            return f"failed: {settings.sms_provider} {r.status_code} {r.text[:120]}"
        return "sent"
    except Exception as exc:  # noqa: BLE001 - never let SMS break the callback
        return f"failed: {exc}"
    finally:
        if own:
            await client.aclose()
