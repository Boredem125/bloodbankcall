from ..config import Settings
from .base import CallProvider, CallStatus
from .bolna import BolnaProvider
from .sim import SimProvider


def build_provider(settings: Settings) -> CallProvider:
    if settings.live_mode:
        if not (settings.bolna_api_key and settings.bolna_agent_id):
            raise RuntimeError("LIVE_MODE needs BOLNA_API_KEY and BOLNA_AGENT_ID")
        return BolnaProvider(settings)
    return SimProvider(settings)


__all__ = ["build_provider", "CallProvider", "CallStatus", "BolnaProvider", "SimProvider"]
