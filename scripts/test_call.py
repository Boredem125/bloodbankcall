"""Place ONE live call to your own (verified) phone and print what the pipeline extracts.

Usage:  .venv\\Scripts\\python.exe scripts\\test_call.py +91XXXXXXXXXX [O-] [units] [--retry N]
Answer and play blood bank staff. This spends Bolna credit (the agent hangs up within 150 s).
--retry N asks Bolna to redial up to N times (1 minute apart) on no-answer/busy; each retry is billed.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402
from app.parsing import parse_fee, parse_staff, parse_units, readback_confirmed, vendor_groups  # noqa: E402
from app.providers.bolna import BolnaProvider, retry_config  # noqa: E402
from app.schemas import COMPONENTS, GROUP_CONFIRM, GROUP_SPOKEN, TERMINAL_STATUSES, Facility, Phone  # noqa: E402


async def main(number: str, group: str, units: int, retries: int = 0) -> None:
    s = get_settings()
    if not (s.bolna_api_key and s.bolna_agent_id):
        sys.exit("Set BOLNA_API_KEY and BOLNA_AGENT_ID in .env first.")
    p = BolnaProvider(s, retry=retry_config(retries) if retries else None)
    phone = Phone(number=number, label="blood bank")
    facility = Facility(id="me", name="Test Blood Centre", area="HSR Layout", lat=12.91, lon=77.64, phones=[phone])
    exec_id = await p.start(facility, phone, {
        "facility_name": facility.name, "phone_label": phone.label,
        "blood_group_spoken": GROUP_SPOKEN[group], "blood_group_confirm": GROUP_CONFIRM[group],
        "component": COMPONENTS["prbc"], "units": str(units), "hospital": "a hospital",
        "area": "HSR Layout", "language": "English or Hindi",
    })
    print("execution_id:", exec_id)
    last = None
    while True:
        await asyncio.sleep(4)
        st = await p.status(exec_id)
        if st.status != last:
            print("status:", st.status)
            last = st.status
        if st.status in TERMINAL_STATUSES or st.status == "voicemail":
            break
    t = st.transcript
    print("\n" + t)
    print(f"\ngroups heard={vendor_groups(t)} readback confirmed={readback_confirmed(t, group)} "
          f"units={parse_units(t)} fee={parse_fee(t)} staff={parse_staff(t)}")
    print(f"duration={st.duration_s}s cost={st.cost_cents} cents recording={st.recording_url}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    retries = 0
    if "--retry" in argv:
        i = argv.index("--retry")
        retries = int(argv[i + 1])
        del argv[i:i + 2]
    if not argv or not argv[0].startswith("+"):
        sys.exit("Usage: test_call.py +91XXXXXXXXXX [O-] [units] [--retry N]")
    grp = argv[1] if len(argv) > 1 else "O-"
    if grp not in GROUP_SPOKEN:
        sys.exit(f"Blood group must be one of {list(GROUP_SPOKEN)}")
    asyncio.run(main(argv[0], grp, int(argv[2]) if len(argv) > 2 else 2, retries))
