"""Place ONE live call to your own phone and print transcript, parsed price and cost.

Usage:  .venv\\Scripts\\python.exe scripts\\test_call.py +91XXXXXXXXXX
Answer the phone and play a shopkeeper: give a price, mention a visiting charge, etc.
This spends Bolna credit (the agent hangs up after 90 s at most).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402
from app.parsing import parse_price, parse_slot  # noqa: E402
from app.providers.bolna import BolnaProvider  # noqa: E402
from app.schemas import TERMINAL_STATUSES, Vendor  # noqa: E402


async def main(phone: str) -> None:
    s = get_settings()
    if not (s.bolna_api_key and s.bolna_agent_id):
        sys.exit("Set BOLNA_API_KEY and BOLNA_AGENT_ID in .env first.")
    p = BolnaProvider(s)
    vendor = Vendor(id="me", name="Test AC Services", category="ac_repair", area="HSR Layout", rating=4.5, phone=phone)
    exec_id = await p.start(vendor, {
        "business_name": vendor.name, "task": "AC service, cooling is weak", "area": "HSR Layout",
        "time_window": "tomorrow evening", "budget": "800", "customer_name": s.customer_name,
        "language": "Hindi or English", "mode": "quote",
    })
    print("execution_id:", exec_id)
    last = None
    while True:
        await asyncio.sleep(4)
        st = await p.status(exec_id)
        if st.status != last:
            print("status:", st.status)
            last = st.status
        if st.status in TERMINAL_STATUSES:
            break
    print("\n" + st.transcript)
    print(f"\nprice={parse_price(st.transcript, st.extracted)} slot={parse_slot(st.transcript, st.extracted)}")
    print(f"duration={st.duration_s}s cost={st.cost_cents} cents recording={st.recording_url}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("+"):
        sys.exit("Usage: test_call.py +91XXXXXXXXXX")
    asyncio.run(main(sys.argv[1]))
