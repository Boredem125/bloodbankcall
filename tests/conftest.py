import pytest

from app.config import Settings
from app.decision import build_engine
from app.orchestrator import Orchestrator
from app.providers import SimProvider
from app.store import Store
from app.vendors import load_vendors


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, sim_speed=200, sim_poll_s=0.02, decision_backend="mock")


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def orch(settings, store) -> Orchestrator:
    return Orchestrator(settings, SimProvider(settings), build_engine(settings), store, load_vendors(settings.vendors_file))
