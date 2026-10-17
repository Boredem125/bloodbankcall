import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.providers.bolna import BolnaProvider
from app.schemas import Vendor

VENDOR = Vendor(id="v", name="Test Biz", category="ac_repair", area="x", rating=4.0, phone="+919999999999")


@respx.mock
async def test_bolna_start_and_status():
    s = Settings(_env_file=None, bolna_api_key="key", bolna_agent_id="agent-1", bolna_from_number="+918000000000")
    call = respx.post("https://api.bolna.ai/call").mock(
        return_value=httpx.Response(200, json={"message": "done", "status": "queued", "execution_id": "ex-1"})
    )
    respx.get("https://api.bolna.ai/executions/ex-1").mock(
        return_value=httpx.Response(200, json={
            "id": "ex-1", "status": "completed", "transcript": "user: 700 rupees",
            "total_cost": 3.2, "conversation_duration": 41,
            "telephony_data": {"recording_url": "https://rec"},
            "extracted_data": {"Quote": {"quoted_price": {"objective": "700"}}},
        })
    )
    p = BolnaProvider(s)
    assert await p.start(VENDOR, {"task": "ac"}) == "ex-1"
    body = httpx.Response(200, content=call.calls.last.request.content).json()
    assert body == {"agent_id": "agent-1", "recipient_phone_number": "+919999999999",
                    "user_data": {"task": "ac"}, "from_phone_number": "+918000000000"}
    st = await p.status("ex-1")
    assert st.status == "completed" and st.cost_cents == 3.2 and st.recording_url == "https://rec"
    assert st.extracted == {"quoted_price": "700"}


def test_api_smoke():
    from app.main import app

    with TestClient(app) as client:
        assert "Phonewala" in client.get("/").text
        r = client.post("/errands", json={"task": "kitchen tap leaking", "budget": 600})
        assert r.status_code == 200
        eid = r.json()["id"]
        assert client.get(f"/errands/{eid}").json()["request"]["budget"] == 600
        assert client.post("/errands", json={"task": "x"}).status_code == 422
