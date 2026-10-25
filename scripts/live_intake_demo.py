"""Live voice-in/voice-out demo on a trial account, without an inbound number or a tunnel.

The intake agent CALLS the family phone (trial accounts cannot receive calls on a number we don't
have), the family plays the caller, the agent hangs up, and this script fetches the finished
execution (transcript + extractions) and posts it to the running app's webhook. The app then does
what it would do for a real inbound call: search (the other verified phone plays the blood bank),
SMS, and the callback agent rings the family phone back.

Usage:  .venv\\Scripts\\python.exe scripts\\live_intake_demo.py +91XXXXXXXXXX [--url http://localhost:8000]
Paid: one intake call + the blood bank call(s) + one callback. Have the app running with LIVE_MODE=true.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402
from app.schemas import TERMINAL_STATUSES  # noqa: E402


async def main(family: str, app_url: str) -> None:
    s = get_settings()
    if not (s.bolna_api_key and s.bolna_intake_agent_id):
        sys.exit("Set BOLNA_API_KEY and BOLNA_INTAKE_AGENT_ID in .env first.")
    try:
        httpx.get(f"{app_url}/searches?limit=1", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"The app is not reachable at {app_url} ({exc}). Start it first: python -m uvicorn app.main:app --port 8000")
    async with httpx.AsyncClient(base_url=s.bolna_base_url, headers={"Authorization": f"Bearer {s.bolna_api_key}"},
                                 timeout=30) as c:
        body = {"agent_id": s.bolna_intake_agent_id, "recipient_phone_number": family, "user_data": {}}
        if s.bolna_from_number:
            body["from_phone_number"] = s.bolna_from_number
        r = await c.post("/call", json=body)
        if r.status_code >= 400:
            sys.exit(f"bolna /call {r.status_code}: {r.text[:300]}")
        exec_id = r.json().get("execution_id") or r.json().get("id")
        print(f"Intake call placed to {family[:3]}XXXXXX{family[-4:]}; execution {exec_id}. Answer and play the family.")
        last = None
        while True:
            await asyncio.sleep(4)
            r = await c.get(f"/executions/{exec_id}")
            if r.status_code >= 400:
                print("poll failed:", r.status_code)
                continue
            data = r.json()
            if data.get("status") != last:
                last = data.get("status")
                print("status:", last)
            if last in TERMINAL_STATUSES:
                break
        # extractions can land a few seconds after "completed"
        for _ in range(6):
            if data.get("extracted_data"):
                break
            await asyncio.sleep(5)
            data = (await c.get(f"/executions/{exec_id}")).json()
    data.setdefault("user_number", family)
    data.setdefault("telephony_data", {})
    data["telephony_data"].setdefault("call_type", "inbound")
    data["telephony_data"].setdefault("from_number", family)
    print("\ntranscript:\n" + (data.get("transcript") or "(none)"))
    print("\nextracted:", data.get("extracted_data"))
    token = f"?token={s.webhook_token}" if s.webhook_token else ""
    r = httpx.post(f"{app_url}/webhooks/bolna{token}", json=data, timeout=30)
    print("\napp said:", r.status_code, r.text)
    print(f"Watch the search and the callback on {app_url}/ (Incoming calls panel).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("family", help="verified phone that plays the family, E.164")
    ap.add_argument("--url", default="http://localhost:8000")
    a = ap.parse_args()
    if not a.family.startswith("+"):
        sys.exit("Phone must be E.164, e.g. +919876543210")
    asyncio.run(main(a.family, a.url))
