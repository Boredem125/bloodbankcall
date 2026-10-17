import importlib.util
import re
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.providers.bolna import BolnaProvider, execution_id_of, merge_execution, normalize_execution, retry_config
from app.schemas import Facility, Phone

ROOT = Path(__file__).resolve().parent.parent

PHONE = Phone(number="+919999999999")
FACILITY = Facility(id="f", name="Test Bank", area="HSR Layout", lat=0, lon=0, phones=[PHONE])


@respx.mock
async def test_bolna_start_and_status():
    s = Settings(_env_file=None, bolna_api_key="key", bolna_agent_id="agent-1", bolna_from_number="+918000000000")
    call = respx.post("https://api.bolna.ai/call").mock(
        return_value=httpx.Response(200, json={"message": "done", "status": "queued", "execution_id": "ex-1"})
    )
    respx.get("https://api.bolna.ai/executions/ex-1").mock(
        return_value=httpx.Response(200, json={
            "id": "ex-1", "status": "completed", "transcript": "user: 3 units",
            "total_cost": 3.2, "conversation_duration": 41,
            "telephony_data": {"recording_url": "https://rec"},
            "extracted_data": {"Stock": {"units": {"objective": "3"}}},
        })
    )
    p = BolnaProvider(s)
    assert await p.start(FACILITY, PHONE, {"blood_group_spoken": "O negative"}) == "ex-1"
    body = httpx.Response(200, content=call.calls.last.request.content).json()
    assert body == {"agent_id": "agent-1", "recipient_phone_number": "+919999999999",
                    "user_data": {"blood_group_spoken": "O negative"}, "from_phone_number": "+918000000000"}
    st = await p.status("ex-1")
    assert st.status == "completed" and st.cost_cents == 3.2 and st.recording_url == "https://rec"
    assert st.extracted == {"units": "3"}


def test_voicemail_is_its_own_status():
    st = normalize_execution({"status": "completed", "answered_by_voice_mail": True, "transcript": "beep"})
    assert st.status == "voicemail"


def test_api_smoke():
    from app.main import app

    with TestClient(app) as client:
        assert "BloodBankCall" in client.get("/").text
        r = client.post("/searches", json={"blood_group": "B+", "units": 1, "area": "Koramangala"})
        assert r.status_code == 200
        sid = r.json()["id"]
        assert client.get(f"/searches/{sid}").json()["request"]["blood_group"] == "B+"
        assert client.post("/searches", json={"blood_group": "X+"}).status_code == 422
        assert client.post("/searches", json={"units": 50}).status_code == 422


def _settings() -> Settings:
    return Settings(_env_file=None, bolna_api_key="key", bolna_agent_id="agent-1")


def _sent(route) -> dict:
    return httpx.Response(200, content=route.calls.last.request.content).json()


# ------------------------------------------------------------------ retries (opt-in)
@respx.mock
async def test_retry_config_is_opt_in():
    call = respx.post("https://api.bolna.ai/call").mock(return_value=httpx.Response(200, json={"execution_id": "ex-1"}))
    await BolnaProvider(_settings()).start(FACILITY, PHONE, {})
    assert "retry_config" not in _sent(call)
    await BolnaProvider(_settings(), retry=retry_config(2, [1, 2])).start(FACILITY, PHONE, {})
    assert _sent(call)["retry_config"] == {
        "enabled": True, "max_retries": 2, "retry_on_statuses": ["no-answer", "busy"],
        "retry_intervals_minutes": [1, 2], "retry_on_voicemail": False, "prioritize_retries": True,
    }


def test_retry_config_validates_bolna_limits():
    with pytest.raises(ValueError):
        retry_config(4)
    with pytest.raises(ValueError):
        retry_config(1, statuses=("voicemail",))
    with pytest.raises(ValueError):
        retry_config(2, [1])


@respx.mock
async def test_no_answer_is_not_final_while_bolna_will_retry():
    route = respx.get("https://api.bolna.ai/executions/ex-1")
    p = BolnaProvider(_settings(), retry=retry_config(1))
    route.mock(return_value=httpx.Response(200, json={"id": "ex-1", "status": "no-answer", "retry_count": 0}))
    assert (await p.status("ex-1")).status == "retrying"
    route.mock(return_value=httpx.Response(200, json={"id": "ex-1", "status": "no-answer", "retry_count": 1}))
    assert (await p.status("ex-1")).status == "no-answer"
    # without retries, no-answer is final straight away
    route.mock(return_value=httpx.Response(200, json={"id": "ex-1", "status": "no-answer", "retry_count": 0}))
    assert (await BolnaProvider(_settings()).status("ex-1")).status == "no-answer"


@respx.mock
async def test_start_rejects_response_without_execution_id():
    respx.post("https://api.bolna.ai/call").mock(return_value=httpx.Response(200, json={"message": "queued"}))
    with pytest.raises(RuntimeError):
        await BolnaProvider(_settings()).start(FACILITY, PHONE, {})


# ------------------------------------------------------------------ webhook ingestion
def test_execution_id_variants():
    assert execution_id_of({"id": "a"}) == "a"
    assert execution_id_of({"execution_id": "b"}) == "b"
    assert execution_id_of({"data": {"id": "c"}}) == "c"
    assert execution_id_of({"status": "queued"}) is None


@respx.mock
async def test_partial_webhooks_merge_and_never_downgrade():
    p = BolnaProvider(_settings())
    assert p.ingest_webhook({"execution_id": "ex-9", "status": "in-progress", "telephony_data": {"to_number": "+91"}}) == "ex-9"
    p.ingest_webhook({"id": "ex-9", "status": "completed", "transcript": "user: 2 units", "total_cost": 6.1,
                      "telephony_data": {"recording_url": "https://rec"}, "extracted_data": None})
    p.ingest_webhook({"id": "ex-9", "status": "call-disconnected", "transcript": ""})  # late, out of order
    cached = p.webhook_cache["ex-9"]
    assert cached["status"] == "completed" and cached["transcript"] == "user: 2 units"
    assert cached["telephony_data"] == {"to_number": "+91", "recording_url": "https://rec"}
    st = await p.status("ex-9")  # served from the cache: respx fails any unmocked GET
    assert st.status == "completed" and st.cost_cents == 6.1 and st.recording_url == "https://rec"


@respx.mock
async def test_non_final_webhook_still_polls_and_merges():
    respx.get("https://api.bolna.ai/executions/ex-3").mock(
        return_value=httpx.Response(200, json={"id": "ex-3", "status": "in-progress", "transcript": ""}))
    p = BolnaProvider(_settings())
    p.ingest_webhook({"data": {"id": "ex-3", "status": "ringing"}})
    p.ingest_webhook({"id": "ex-3", "status": "in-progress", "transcript": "assistant: Namaste"})
    st = await p.status("ex-3")
    assert st.status == "in-progress" and st.transcript == "assistant: Namaste"


def test_ingest_ignores_unusable_payloads():
    p = BolnaProvider(_settings())
    assert p.ingest_webhook({"status": "queued"}) is None and p.webhook_cache == {}


def test_auto_retry_webhook_reopens_a_final_status():
    old = {"id": "e", "status": "no-answer", "retry_count": 0}
    assert merge_execution(old, {"id": "e", "status": "scheduled", "retry_count": 1})["status"] == "scheduled"
    assert merge_execution(old, {"id": "e", "status": "queued"})["status"] == "no-answer"


def test_webhook_endpoint_accepts_variants():
    from app.main import app

    with TestClient(app) as client:
        for payload in ({"id": "w1", "status": "completed"}, {"execution_id": "w2"}, {"status": "queued"}):
            assert client.post("/webhooks/bolna", json=payload).json() == {"ok": True}


# ------------------------------------------------------------------ agent config
def _create_agent():
    spec = importlib.util.spec_from_file_location("create_agent", ROOT / "scripts" / "create_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _task(body: dict) -> dict:
    return body["agent_config"]["tasks"][0]


def test_english_profile_keeps_validated_config():
    ca = _create_agent()
    tools = _task(ca.agent_body(None))["tools_config"]
    t = dict(tools["transcriber"])
    assert t.pop("keywords") == ca.KEYWORDS
    assert t == {"provider": "deepgram", "model": "nova-3", "language": "en", "stream": True,
                 "encoding": "linear16", "sampling_rate": 16000, "endpointing": 250}
    assert tools["synthesizer"]["provider_config"]["voice_id"] == "IkSv4tkouLJ6kYsQA7XD"
    assert _task(ca.agent_body(None))["task_config"] == {"call_terminate": 150, "hangup_after_silence": 15}
    assert "keywords" not in _task(ca.agent_body(None, keywords=False))["tools_config"]["transcriber"]


def test_keywords_fit_provider_limits():
    ca = _create_agent()
    terms = ca.KEYWORDS.split(",")
    assert len(terms) <= 50 and all(0 < len(x) <= 64 for x in terms)  # Sarvam saaras:v4 limits
    for word in ("positive", "negative", "plus", "minus", "O", "A", "B", "AB", "PRBC", "packed cells",
                 "platelets", "plasma", "units", "replacement", "donor"):
        assert word in terms


@pytest.mark.parametrize("profile", ["hi", "kn"])
def test_indic_profiles_use_sarvam_and_need_a_listed_voice(profile):
    ca = _create_agent()
    with pytest.raises(ValueError, match="--list-voices"):
        ca.agent_body(None, profile)
    tools = _task(ca.agent_body(None, profile, "voice_x", "Voice X"))["tools_config"]
    assert tools["transcriber"]["provider"] == "sarvam" and tools["transcriber"]["model"] == "saaras:v4"
    assert tools["transcriber"]["language"] == profile and tools["transcriber"]["keywords"] == ca.KEYWORDS
    assert tools["synthesizer"]["provider"] == "sarvam"
    assert tools["synthesizer"]["provider_config"] == {"voice": "Voice X", "voice_id": "voice_x",
                                                       "model": "bulbul:v4-flash", "language": profile, "speed": 1.0}


def test_noise_cancellation_is_opt_in_and_bounded():
    ca = _create_agent()
    assert _task(ca.agent_body(None, noise_cancellation=75))["task_config"]["noise_cancellation_level"] == 75
    with pytest.raises(ValueError):
        ca.agent_body(None, noise_cancellation=40)


@respx.mock
def test_list_voices_only_reads():
    ca = _create_agent()
    respx.get("https://api.bolna.ai/api/v1/voice-config/tts").mock(return_value=httpx.Response(200, json={
        "language": "hi", "providers": [
            {"id": "p1", "slug": "sarvam", "is_supported": True,
             "models": [{"id": "m1", "model_id": "bulbul:v4-flash", "is_supported": True}]},
            {"id": "p2", "slug": "elevenlabs", "is_supported": False, "models": [{"id": "m2", "is_supported": True}]},
        ]}))
    voices = respx.get("https://api.bolna.ai/api/v1/voice-config/tts/voices").mock(return_value=httpx.Response(
        200, json={"items": [{"voice_id": "aparna_hi_customer", "name": "Aparna (Customer)", "gender": "female"}]}))
    with httpx.Client(base_url="https://api.bolna.ai") as client:
        rows = ca.list_voices(client, "hi")
    assert rows == [{"provider": "sarvam", "model": "bulbul:v4-flash", "voice_id": "aparna_hi_customer",
                     "voice": "Aparna (Customer)", "gender": "female", "source": None}]
    assert voices.calls.last.request.url.params["provider_id"] == "p1"
    assert all(c.request.method == "GET" for c in respx.calls)


def test_prompt_keeps_variables_and_readback():
    prompt = (ROOT / "agent" / "blood_bank_caller_prompt.md").read_text(encoding="utf-8")
    for var in ("facility_name", "phone_label", "blood_group_spoken", "blood_group_confirm", "component",
                "units", "hospital", "area", "language"):
        assert "{{" + var + "}}" in prompt
    assert 'Read back exactly: "Confirming: {{blood_group_spoken}}, that is {{blood_group_confirm}}' in prompt
    assert re.search(r"Confirming: O negative, matlab O minus", prompt)
