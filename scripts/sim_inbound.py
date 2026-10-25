"""Pretend a family just phoned in (no Bolna number needed). Free; the app must be running on :8000.

Usage:  .venv\\Scripts\\python.exe scripts\\sim_inbound.py [O-] [units] [area] [--caller +91XXXXXXXXXX] [--transcript file.txt]
With --transcript, extractions are left empty and the app parses the transcript text instead.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blood_group", nargs="?", default="O-")
    ap.add_argument("units", nargs="?", type=int, default=2)
    ap.add_argument("area", nargs="?", default="HSR Layout")
    ap.add_argument("--caller", default="+919999900001")
    ap.add_argument("--hospital", default="")
    ap.add_argument("--transcript", type=Path)
    ap.add_argument("--url", default="http://localhost:8000")
    a = ap.parse_args()
    body = {"caller": a.caller, "blood_group": a.blood_group, "units": a.units, "area": a.area, "hospital": a.hospital}
    if a.transcript:
        body["transcript"] = a.transcript.read_text(encoding="utf-8")
    r = httpx.post(f"{a.url}/intake/simulate", json=body, timeout=30)
    if r.status_code >= 400:
        sys.exit(f"{r.status_code}: {r.text}")
    print(json.dumps(r.json(), indent=2))
    print(f"Watch it: {a.url}/  (Incoming calls panel)")


if __name__ == "__main__":
    main()
