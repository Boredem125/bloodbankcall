from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Calls. LIVE_MODE=false keeps every call simulated and free.
    live_mode: bool = False
    bolna_api_key: str = ""
    bolna_agent_id: str = ""
    bolna_from_number: str = ""
    bolna_base_url: str = "https://api.bolna.ai"
    max_live_calls_per_search: int = 4  # hard cap on paid calls per search (fallback numbers count)
    live_concurrency: int = 2  # Bolna account concurrency (GET /user/me -> concurrency.max)
    poll_interval_s: float = 4.0
    call_timeout_s: float = 300.0

    # Phone intake: a family calls BOLNA_INBOUND_NUMBER, the intake agent takes the request, Bolna's
    # webhook brings us the transcript + extractions, we search, then the callback agent rings them back.
    bolna_intake_agent_id: str = ""  # scripts/create_intake_agent.py
    bolna_callback_agent_id: str = ""
    bolna_inbound_number: str = ""  # the number families call (bought/attached on Bolna); shown on the dashboard
    webhook_token: str = ""  # if set, /webhooks/bolna requires ?token=<this>
    callback_delay_s: float = 0.0  # wait before ringing the family back (their line may still be busy)
    sms_provider: str = "none"  # none | plivo | twilio
    sms_auth_id: str = ""  # Plivo auth id / Twilio account sid
    sms_auth_token: str = ""
    sms_from: str = ""  # sender number (E.164) or Indian sender id

    # Decisions: jev | clef | mock. Failures fall back down the chain; mock answers are
    # never trusted to confirm availability during live calls.
    decision_backend: str = "mock"
    typesafe_api_key: str = ""
    typesafe_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_model: str = "jev-latest"
    cf_account_id: str = ""
    cf_api_token: str = ""
    clef_model: str = "clef-flash"

    # Search
    wave_size: int = 6  # simulated calls per wave (live waves use live_concurrency)
    min_confirmed: int = 2  # stop calling once this many confirm and units are covered
    search_deadline_s: float = 240.0  # no new waves after this; an emergency can't wait
    sim_speed: float = 1.0  # >1 makes simulated calls faster
    sim_poll_s: float = 0.5

    # Load, cost and abuse limits
    cache_ttl_min: float = 20.0  # reuse a "confirmed"/"not available" answer this fresh instead of calling again
    max_calls_per_number_per_hour: int = 4  # across all searches; protects blood bank phone lines
    max_searches_per_client_per_hour: int = 5  # per request IP; more get HTTP 429
    max_concurrent_searches: int = 3  # searches running at once, all clients
    trust_forwarded_for: bool = False  # use X-Forwarded-For as the client IP (only behind your own proxy)
    pickup_stats_file: str = ""  # e.g. data/pickup_stats.json to keep learned pickup rates; empty = memory only

    facilities_file: Path = ROOT / "data" / "facilities.json"
    sim_scripts_file: Path = ROOT / "data" / "sim_scripts.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
