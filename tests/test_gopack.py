from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from app.gopack import build_gopack, hold_text
from app.schemas import BloodRequest, CallRecord, Facility, Phone, PlanItem, Search

TRANSCRIPT = (
    "assistant: Do you have O negative, that is O minus?\n"
    "user: Yes, O negative is available. We have 4 units.\n"
    "user: Yes, we can hold it for one hour. This is Ramesh.\n"
    "user: Yes, correct."
)


def call(name, units=4, staff="Ramesh", fee=1050, availability="confirmed", donor=0.1, hold=0.8, eta=12, **kw):
    f = Facility(id=name, name=name, area="HSR Layout", lat=0, lon=0, phones=[Phone(number=f"+91 80 {len(name)}000")])
    return CallRecord(facility=f, phone=f.phones[0], status="completed", outcome="available", transcript=TRANSCRIPT,
                      units=units, staff=staff, fee=fee, eta_min=eta, availability=availability, confirmed_at=1_700_000_000,
                      signals={"replacement_donor_required": donor, "will_hold": hold}, **kw)


def done_search(calls, plan, units=2, hospital=""):
    return Search(request=BloodRequest(blood_group="O-", units=units, hospital=hospital), status="done", calls=calls,
                  chosen_call_id=calls[0].id, plan=plan, warnings=["Confirm at the blood bank counter on arrival."])


def test_confirmed_gopack():
    c = call("Sanjeevani Blood Centre")
    s = done_search([c], [PlanItem(call_id=c.id, facility=c.facility.name, units=2)], hospital="City Hospital")
    g = build_gopack(s)
    assert g.status == "done" and g.units_covered == 2
    f = g.facility
    assert (f.name, f.area, f.staff, f.fee, f.units) == ("Sanjeevani Blood Centre", "HSR Layout", "Ramesh", 1050, 2)
    assert f.hold == "Kept aside for one hour" and f.confirmed_at_text and not f.replacement_donor_required
    assert f.maps_url == "https://www.google.com/maps/search/?api=1&query=Sanjeevani%20Blood%20Centre%2C%20HSR%20Layout"
    assert any("requisition" in d for d in g.documents) and any("ID" in d for d in g.documents)
    assert not any("donor" in d for d in g.documents)
    assert "Sanjeevani Blood Centre" in g.message and c.phone.number in g.message and "City Hospital" in g.message
    assert unquote(g.whatsapp_url.removeprefix("https://wa.me/?text=")) == g.message
    assert g.sms_url.startswith("sms:?&body=") and unquote(g.sms_url.split("body=", 1)[1]) == g.message
    assert g.leads == [] and g.alternatives is None


def test_replacement_donor_and_no_hold():
    c = call("Donor Bank", donor=0.9, hold=0.1)
    g = build_gopack(done_search([c], [PlanItem(call_id=c.id, facility=c.facility.name, units=2)]))
    assert g.facility.replacement_donor_required and g.facility.hold == "Not kept aside: go quickly"
    assert any("replacement donor" in d for d in g.documents)
    assert "Bring a replacement donor." in g.message


def test_split_plan_lists_each_pickup():
    a, b = call("Alpha Bank", units=1, staff="Asha"), call("Beta Bank", units=5, staff=None, fee=None)
    s = done_search([a, b], [PlanItem(call_id=a.id, facility="Alpha Bank", units=1),
                             PlanItem(call_id=b.id, facility="Beta Bank", units=2)], units=3)
    g = build_gopack(s)
    assert [(p.name, p.units) for p in g.pickups] == [("Alpha Bank", 1), ("Beta Bank", 2)]
    assert g.facility.name == "Alpha Bank" and g.units_covered == 3
    assert "1) 1 unit(s) from:" in g.message and "2) 2 unit(s) from:" in g.message
    assert a.phone.number in g.message and b.phone.number in g.message


def test_failed_lists_leads_and_doctor_approval():
    lead = call("Maybe Bank", availability="unconfirmed", gate_reasons=["staff did not confirm the read-back"])
    no = call("No Bank", availability="not_available")
    s = Search(request=BloodRequest(blood_group="B+", component="prbc"), status="failed", calls=[lead, no])
    g = build_gopack(s)
    assert g.facility is None and [l.name for l in g.leads] == ["Maybe Bank"]
    assert g.leads[0].reasons == ["staff did not confirm the read-back"]
    assert "Maybe Bank" in g.message and lead.phone.number in g.message and "No Bank" not in g.message
    assert "NOT confirmed" in g.message and "donor groups" in g.message
    assert g.alternatives.groups == ["B-", "O+", "O-"]
    assert "treating doctor approves" in g.alternatives.warning and "treating doctor approves" in g.message


def test_failed_o_negative_has_no_alternative_line():
    g = build_gopack(Search(request=BloodRequest(blood_group="O-"), status="failed"))
    assert g.alternatives.groups == [] and "another group" not in g.message and "donor groups" in g.message


def test_message_has_no_patient_fields():
    c = call("Sanjeevani Blood Centre")
    g = build_gopack(done_search([c], [PlanItem(call_id=c.id, facility=c.facility.name, units=2)]))
    assert "patient at" not in g.message  # no hospital given: nothing about the patient


def test_hold_text_without_duration():
    c = call("X")
    c.transcript = "user: Okay, we will keep it aside."
    assert hold_text(c) == "Agreed to keep it aside"


def test_running_search_rejected():
    with pytest.raises(ValueError):
        build_gopack(Search(request=BloodRequest(), status="calling"))


def test_gopack_route():
    from app.main import app
    from app.store import store

    running = store.add(Search(request=BloodRequest(), status="calling"))
    failed = store.add(Search(request=BloodRequest(blood_group="A+"), status="failed"))
    with TestClient(app) as client:
        assert client.get("/searches/nope/gopack").status_code == 404
        assert client.get(f"/searches/{running.id}/gopack").status_code == 409
        r = client.get(f"/searches/{failed.id}/gopack")
        assert r.status_code == 200 and r.json()["alternatives"]["groups"] == ["A-", "O+", "O-"]
        assert parse_qs(urlparse(r.json()["whatsapp_url"]).query)["text"][0] == r.json()["message"]
