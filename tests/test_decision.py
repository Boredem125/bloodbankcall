import httpx
import pytest
import respx

from app.config import Settings
from app.decision import build_engine
from app.decision.base import DecisionError, FallbackEngine, choice, noul, score
from app.decision.http import ClefEngine, JevEngine
from app.decision.mock import MockEngine
from app.decision.questions import call_questions
from app.schemas import BloodRequest

REQ = BloodRequest(blood_group="O-", units=2)
QS = call_questions(REQ)
ANSWER = {"model": "jev-1.13.0", "answers": {"has_requested": {"type": "noul", "noul": 0.97}}}


def state(transcript, units=None, heard=()):
    return {"request": {"blood_group": "O-", "component": "PRBC", "units_needed": 2}, "facility": "x",
            "parsed": {"units_said": units, "groups_heard": list(heard), "fee_rupees": None}, "transcript": transcript}


async def test_mock_reads_a_clear_yes():
    t = ("assistant: Do you have O negative, that is O minus?\nuser: yes O negative available, 3 units\n"
         "assistant: Confirming: O negative, that is O minus, 3 units, correct?\nuser: yes correct")
    a = await MockEngine().decide(state(t, units=3), QS)
    assert noul(a, "has_requested") > 0.9 and noul(a, "confirmed_readback") > 0.9
    assert noul(a, "mentioned_other_group") < 0.3 and choice(a, "outcome")[0] == "available"


async def test_mock_flags_other_group():
    t = "assistant: Do you have O negative?\nuser: sorry O negative is not available, only B positive"
    a = await MockEngine().decide(state(t), QS)
    assert noul(a, "has_requested") < 0.5 and noul(a, "mentioned_other_group") > 0.5
    assert choice(a, "outcome")[0] == "not_available"


async def test_mock_detects_replacement_and_closed():
    t = "assistant: Is a donor needed?\nuser: you must bring a replacement donor. Blood bank closed now, opens at 9 am"
    a = await MockEngine().decide(state(t), QS)
    assert noul(a, "replacement_donor_required") > 0.5 and noul(a, "open_now") < 0.3


def test_questions_name_the_requested_group():
    assert "O negative" in QS["has_requested"]["instructions"]
    assert "at least 2 units" in QS["enough_units"]["instructions"]


class Boom:
    name = "boom"

    async def decide(self, state, questions):
        raise RuntimeError("down")


async def test_fallback_uses_next_engine_and_reports_it():
    answers, backend, ms = await FallbackEngine([Boom(), MockEngine()]).decide(state("user: hello"), QS)
    assert backend == "mock" and set(answers) == set(QS) and ms >= 0


async def test_fallback_raises_when_all_fail():
    with pytest.raises(DecisionError):
        await FallbackEngine([Boom()]).decide({}, QS)


def test_chain_order_respects_keys():
    s = Settings(_env_file=None, decision_backend="jev", typesafe_api_key="k", cf_account_id="a", cf_api_token="t")
    assert [e.name for e in build_engine(s).chain] == ["jev", "clef", "mock"]
    assert [e.name for e in build_engine(Settings(_env_file=None, decision_backend="jev")).chain] == ["mock"]


@respx.mock
async def test_jev_request_shape():
    route = respx.post("https://api.typesafe.ai/v1/systemone").mock(return_value=httpx.Response(200, json=ANSWER))
    answers = await JevEngine(Settings(_env_file=None, typesafe_api_key="secret")).decide("x", {"has_requested": QS["has_requested"]})
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer secret"
    body = httpx.Response(200, content=sent.content).json()
    assert body["model"] == "jev-latest" and list(body["questions"]) == ["has_requested"]
    assert noul(answers, "has_requested") == 0.97


@respx.mock
async def test_clef_unwraps_cloudflare_result():
    url = "https://api.cloudflare.com/client/v4/accounts/acc/ai/run/@cf/cloudflare/clef-flash"
    respx.post(url).mock(return_value=httpx.Response(200, json={"success": True, "result": ANSWER}))
    answers = await ClefEngine(Settings(_env_file=None, cf_account_id="acc", cf_api_token="tok")).decide("x", {})
    assert noul(answers, "has_requested") == 0.97


@respx.mock
async def test_http_error_raises():
    respx.post("https://api.typesafe.ai/v1/systemone").mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(DecisionError):
        await JevEngine(Settings(_env_file=None, typesafe_api_key="k")).decide("x", QS)


def test_score_normalised():
    assert score({"r": {"score": 2}}, "r", 3) == 1.0
