import json
from pathlib import Path

from .schemas import Vendor


def load_vendors(path: Path) -> list[Vendor]:
    return [Vendor(**v) for v in json.loads(path.read_text(encoding="utf-8"))]


def vendors_for(vendors: list[Vendor], category: str) -> list[Vendor]:
    """Vendors in a category, best-rated first (call order)."""
    return sorted((v for v in vendors if v.category == category), key=lambda v: -v.rating)
