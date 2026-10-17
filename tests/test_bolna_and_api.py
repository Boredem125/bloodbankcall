import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.providers.bolna import BolnaProvider, normalize_execution
from app.schemas import Facility, Phone

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
