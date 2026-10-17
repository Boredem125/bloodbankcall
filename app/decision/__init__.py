from ..config import Settings
from .base import DecisionEngine, FallbackEngine
from .http import ClefEngine, JevEngine
from .mock import MockEngine


def build_engine(settings: Settings) -> FallbackEngine:
    """Chain starting at DECISION_BACKEND, always ending in the mock so the demo never stalls."""
    chain: list[DecisionEngine] = []
    order = {"jev": ["jev", "clef"], "clef": ["clef"], "mock": []}.get(settings.decision_backend, [])
    for name in order:
        if name == "jev" and settings.typesafe_api_key:
            chain.append(JevEngine(settings))
        if name == "clef" and settings.cf_account_id and settings.cf_api_token:
            chain.append(ClefEngine(settings))
    chain.append(MockEngine())
    return FallbackEngine(chain)


__all__ = ["build_engine", "FallbackEngine", "DecisionEngine"]
