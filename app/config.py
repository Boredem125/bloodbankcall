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

    facilities_file: Path = ROOT / "data" / "facilities.json"
    sim_scripts_file: Path = ROOT / "data" / "sim_scripts.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
