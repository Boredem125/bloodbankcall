"""Show Bolna wallet balance and concurrency before spending credit. Free to run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings  # noqa: E402


def main() -> None:
    s = get_settings()
    if not s.bolna_api_key:
        sys.exit("Set BOLNA_API_KEY in .env first.")
    r = httpx.get(f"{s.bolna_base_url}/user/me", headers={"Authorization": f"Bearer {s.bolna_api_key}"}, timeout=20)
    r.raise_for_status()
    print(json.dumps(r.json(), indent=2))


if __name__ == "__main__":
    main()
