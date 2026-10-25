"""Phone intake: finished intake call -> request -> search -> callback + SMS."""
from fastapi.testclient import TestClient

from app.config import Settings
from app.intake import IntakeError, PhoneIntake, caller_of, e164, intake_request, spell_number, spoken_plan
from app.notify import sms_request
from app.schemas import mask_number

CALLER = "+919876543210"


def payload(extracted=None, transcript="", caller=CALLER, status="completed", agent_id="intake-1", exec_id="ex-in-1"):
    return {
        "id": exec_id, "status": status, "agent_id": agent_id, "user_number": caller, "transcript": transcript,
        "extracted_data": extracted or {},
        "telephony_data": {"call_type": "inbound", "from_number": caller, "to_number": "+918035739222"},
    }


def extracted(**fields):
    """Bolna's post-migration shape: category -> name -> {objective|subjective, confidence}."""
    out = {}
    for k, v in fields.items():
        out[k] = {"objective": v, "confidence": 0.9} if k in ("blood_group", "component", "language") \
            else {"subjective": v, "objective": None, "confidence": 0.9}
    return {"Blood request": out}


# ----------------------------------------------------------------------------- numbers
def test_e164_normalises_indian_numbers():
    assert e164("98765 43210") == "+919876543210"
    assert e164("09876543210") == "+919876543210"
    assert e164("+91 98765-43210") == "+919876543210"
    assert e164("+14155550100") == "+14155550100"
    assert e164("12345") is None and e164("") is None and e164(None) is None


def test_caller_is_from_number_on_inbound_calls():
    p = payload()
    del p["user_number"]
    assert caller_of(p) == CALLER
    p["telephony_data"]["call_type"] = "outbound"
    p["telephony_data"]["to_number"] = "+919999900002"
    assert caller_of(p) == "+919999900002"


def test_mask_and_spell():
    assert mask_number(CALLER) == "+91XXXXXX3210"
    assert spell_number(CALLER) == "9 8 7 6 5, 4 3 2 1 0"
    assert spell_number("08012345678") == "0 8 0 1 2, 3 4 5 6 7, 8"


# ----------------------------------------------------------------------------- request building
def test_request_from_extractions():
    p = payload(extracted(blood_group="O-", component="prbc", units="3", area="Koramangala",
                          hospital="City Hospital", language="hi", callback_number=""))
    it = intake_request(p)
    r = it.request
    assert (r.blood_group, r.component, r.units, r.area, r.hospital, r.language) == \
        ("O-", "prbc", 3, "Koramangala", "City Hospital", "Hindi")
    assert it.caller == CALLER and it.execution_id == "ex-in-1" and it.notes == []


def test_callback_number_overrides_caller():
    p = payload(extracted(blood_group="B+", callback_number="98765 00000"))
    assert intake_request(p).caller == "+919876500000"


def test_request_from_transcript_when_extractions_missing():
    t = "\n".join([
        "assistant: Which blood group does the patient need?",
        "user: A B negative",
        "assistant: AB negative, that is AB minus, correct?",
        "user: yes. we need two units of platelets. patient is in Jayanagar at Apollo.",
        "assistant: Confirming: AB negative, that is AB minus, platelets, 2 units, near Jayanagar.",
    ])
    r = intake_request(payload(transcript=t)).request
    assert (r.blood_group, r.component, r.units, r.area) == ("AB-", "platelets", 2, "Jayanagar")


def test_assumptions_are_noted_not_guessed():
    it = intake_request(payload(extracted(blood_group="O+")))
    assert it.request.units == 1 and it.request.component == "prbc" and it.request.area == "HSR Layout"
    assert any("units assumed" in n for n in it.notes) and any("area not recognised" in n for n in it.notes)


def test_no_group_is_rejected():
    try:
        intake_request(payload(extracted(blood_group="unclear"), transcript="user: we need blood fast"))
    except IntakeError as exc:
        assert "blood group" in str(exc)
    else:
        raise AssertionError("intake without a blood group must be rejected")


def test_corrected_group_needs_matching_readback():
    t = "\n".join([
        "user: O positive. no sorry, O negative.",
        "assistant: Confirming: O negative, that is O minus, 2 units.",
        "user: yes",
    ])
    assert intake_request(payload(transcript=t)).request.blood_group == "O-"
    # two groups, no read-back agreeing: refuse rather than pick one
    try:
        intake_request(payload(transcript="user: O positive. no, O negative. actually positive."))
    except IntakeError:
        pass
    else:
        raise AssertionError("ambiguous group must be rejected")


def test_units_clamped():
    assert intake_request(payload(extracted(blood_group="O-", units="40"))).request.units == 10


# ----------------------------------------------------------------------------- the flow
def _intake(settings, orch, store, **over) -> PhoneIntake:
    s = Settings(**{**settings.model_dump(), "bolna_intake_agent_id": "intake-1", **over})
    return PhoneIntake(s, orch, store, orch.provider)


def test_accept_filters_and_dedupes(settings, orch, store):
    pi = _intake(settings, orch, store)
    assert pi.accept(payload(status="call-disconnected")) is None
    assert pi.accept(payload(agent_id="caller-agent")) is None  # a blood bank call, not an intake
    s = pi.accept(payload(extracted(blood_group="O-", units="2")))
    assert s is not None and s.source == "phone" and s.caller == CALLER and s.status == "calling"
    assert pi.accept(payload(extracted(blood_group="O-", units="2"))) is None  # same execution id again


def test_accept_without_agent_id_uses_call_type(settings, orch, store):
    pi = _intake(settings, orch, store, bolna_intake_agent_id="")
    assert pi.accept(payload(extracted(blood_group="O-"), agent_id="whatever")) is not None
    p = payload(extracted(blood_group="O-"), agent_id="x", exec_id="ex-out")
    p["telephony_data"]["call_type"] = "outbound"
    assert pi.accept(p) is None


def test_rejected_intake_becomes_failed_search(settings, orch, store):
    pi = _intake(settings, orch, store)
    s = pi.accept(payload(extracted(blood_group="unclear")))
    assert s.status == "failed" and s.source == "phone" and "call again" in s.reason
    assert s.callback_status == "skipped"


async def test_phone_search_runs_and_calls_back(settings, orch, store):
    pi = _intake(settings, orch, store)
    s = pi.accept(payload(extracted(blood_group="O-", units="2", area="HSR Layout")))
    await pi.run(s.id)
    assert s.status == "done"
    chosen = s.call(s.chosen_call_id)
    assert s.callback_status == "completed" and s.callback_execution_id.startswith("sim-")
    assert s.sms_status.startswith("skipped")
    plan = spoken_plan(s)
    assert chosen.facility.name in plan and "O negative" in plan and "http" not in plan
    assert f"They confirmed {chosen.units} units" in plan and chosen.units == 4  # the bank's stock, not our 2-unit share
    assert spell_number(chosen.phone.number) in plan
    assert any("Calling +91XXXXXX3210 back" in l for l in s.log)
    # the callback ran through the sim with the plan text as the agent's line
    rec = orch.provider.calls[s.callback_execution_id]
    assert any(chosen.facility.name in ln for ln in rec.lines)


async def test_failed_search_callback_is_honest(settings, orch, store):
    orch.facilities = [f for f in orch.facilities if f.id in {"hope", "unity", "lifeline"}]
    pi = _intake(settings, orch, store)
    s = pi.accept(payload(extracted(blood_group="O-", units="2")))
    await pi.run(s.id)
    assert s.status == "failed"
    plan = spoken_plan(s)
    assert plan.startswith("I am sorry") and "not confirmed" in plan and "Whitefield Hope" in plan
    assert s.callback_status == "completed"


async def test_live_without_callback_agent_skips_callback(settings, orch, store, monkeypatch):
    monkeypatch.setattr(orch.provider, "name", "bolna")
    pi = _intake(settings, orch, store, bolna_callback_agent_id="")
    s = pi.accept(payload(extracted(blood_group="O-", units="2")))
    await pi.run(s.id)
    assert s.callback_status.startswith("skipped") and s.callback_execution_id is None


# ----------------------------------------------------------------------------- API
def test_api_webhook_token_and_simulated_intake(monkeypatch):
    monkeypatch.setenv("LIVE_MODE", "false")
    monkeypatch.setenv("WEBHOOK_TOKEN", "sekrit")
    monkeypatch.setenv("SIM_SPEED", "200")
    monkeypatch.setenv("SIM_POLL_S", "0.02")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.main import app

    try:
        with TestClient(app) as client:
            assert client.post("/webhooks/bolna", json=payload()).status_code == 403
            assert client.post("/webhooks/bolna?token=sekrit", json={"id": "x", "status": "ringing"}).json() == {"ok": True}
            r = client.post("/intake/simulate", json={"caller": CALLER, "blood_group": "B+", "units": 1, "area": "Jayanagar"})
            assert r.status_code == 200 and r.json()["status"] == "calling"
            sid = r.json()["id"]
            rows = client.get("/searches?limit=5").json()
            mine = next(x for x in rows if x["id"] == sid)
            assert mine["source"] == "phone" and mine["caller"] == "+91XXXXXX3210" and mine["blood_group"] == "B+"
            assert CALLER not in str(rows)
            bad = client.post("/intake/simulate", json={"caller": CALLER, "transcript": "user: need blood"})
            assert bad.status_code == 200 and bad.json()["status"] == "failed"
    finally:
        get_settings.cache_clear()


# ----------------------------------------------------------------------------- SMS
def test_sms_request_shapes():
    s = Settings(_env_file=None, sms_provider="plivo", sms_auth_id="ID", sms_auth_token="T", sms_from="BLDBNK")
    url, body, kw = sms_request(s, "+919876543210", "hi")
    assert url.endswith("/Account/ID/Message/") and body["json"]["dst"] == "919876543210" and kw["auth"] == ("ID", "T")
    s = Settings(_env_file=None, sms_provider="twilio", sms_auth_id="AC", sms_auth_token="T", sms_from="+1555")
    url, body, _ = sms_request(s, "+919876543210", "hi")
    assert "AC/Messages.json" in url and body["data"]["To"] == "+919876543210"
    assert sms_request(Settings(_env_file=None), "+91", "x") is None
