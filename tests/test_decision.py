import httpx
import pytest
import respx

from app.config import Settings
from app.decision import build_engine
from app.decision.base import DecisionError, FallbackEngine, choice, noul, score
from app.decision.http import ClefEngine, JevEngine
from app.decision.mock import MockEngine
from app.decision.questions import intake_questions

JEV_ANSWER = {
    "model": "jev-1.13.0",
    "answers": {
        "category": {"type": "choice", "choice": "plumber", "confidence": 0.9, "probabilities": {"plumber": 0.9}},
        "urgent": {"type": "noul", "noul": 0.2},
    },
}


@pytest.mark.parametrize(
    "task,expected",
    [
        ("AC service tomorrow, cooling is weak", "ac_repair"),
        ("need a plumber, kitchen tap leaking", "plumber"),
        ("PG near Koramangala under 12000", "pg_rental"),
        ("packers and movers on Sunday", "movers"),
    ],
)
async def test_mock_classifies_category(task, expected):
    answers = await MockEngine().decide({"request": task}, intake_questions())
    assert choice(answers, "category")[0] == expected


class Boom:
    name = "boom"

    async def decide(self, state, questions):
        raise RuntimeError("down")


async def test_fallback_uses_next_engine_and_reports_it():
    engine = FallbackEngine([Boom(), MockEngine()])
    answers, backend, ms = await engine.decide({"request": "fix my fan wiring"}, intake_questions())
    assert backend == "mock"
    assert choice(answers, "category")[0] == "electrician"
    assert ms >= 0


async def test_fallback_raises_when_all_fail():
    with pytest.raises(DecisionError):
        await FallbackEngine([Boom()]).decide({}, intake_questions())


def test_chain_order_respects_keys():
    s = Settings(_env_file=None, decision_backend="jev", typesafe_api_key="k", cf_account_id="a", cf_api_token="t")
    assert [e.name for e in build_engine(s).chain] == ["jev", "clef", "mock"]
    s = Settings(_env_file=None, decision_backend="jev")  # no keys: straight to mock
    assert [e.name for e in build_engine(s).chain] == ["mock"]


@respx.mock
async def test_jev_request_shape():
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(return_value=httpx.Response(200, json=JEV_ANSWER))
    s = Settings(_env_file=None, typesafe_api_key="secret")
    answers = await JevEngine(s).decide({"request": "tap leak"}, intake_questions())
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer secret"
    body = httpx.Response(200, content=sent.content).json()
    assert body["model"] == "jev-latest" and set(body["questions"]) == {"category", "urgent"}
    assert noul(answers, "urgent") == 0.2


@respx.mock
async def test_clef_unwraps_cloudflare_result():
    url = "https://api.cloudflare.com/client/v4/accounts/acc/ai/run/@cf/cloudflare/clef-flash"
    respx.post(url).mock(return_value=httpx.Response(200, json={"success": True, "result": JEV_ANSWER}))
    s = Settings(_env_file=None, cf_account_id="acc", cf_api_token="tok")
    answers = await ClefEngine(s).decide("x", intake_questions())
    assert choice(answers, "category") == ("plumber", 0.9)


@respx.mock
async def test_http_error_raises():
    respx.post("https://api.typesafe.ai/v1/systemone").mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(DecisionError):
        await JevEngine(Settings(_env_file=None, typesafe_api_key="k")).decide("x", intake_questions())


def test_score_normalised():
    assert score({"r": {"score": 2}}, "r", 3) == 1.0
    assert score({"r": {"score": 1}}, "r", 3) == 0.5
