import pytest

from app.config import Settings
from app.decision import build_engine
from app.facilities import load_facilities
from app.orchestrator import Orchestrator
from app.providers import SimProvider
from app.store import Store


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, sim_speed=200, sim_poll_s=0.02, poll_interval_s=0.02, decision_backend="mock")


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def orch(settings, store) -> Orchestrator:
    return Orchestrator(settings, SimProvider(settings), build_engine(settings), store, load_facilities(settings.facilities_file))
