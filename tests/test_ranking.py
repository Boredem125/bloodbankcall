from app.ranking import gate, plan_units, rank
from app.schemas import BloodRequest, CallRecord, Facility, Phone

REQ = BloodRequest(blood_group="O-", units=2)
def good_transcript(units=3):
    return (
        "assistant: Do you have O negative, that is O minus?\n"
        f"user: yes O negative is there, {units} units\n"
        f"assistant: Confirming: O negative, that is O minus, {units} units, correct?\n"
        "user: yes correct"
    )


def call(name="Bank", eta=10, units=3, transcript=None, heard=("O-",), **signals):
    # Transcript reads back the same unit count the record holds; the gate now checks they agree.
    transcript = good_transcript(units) if transcript is None else transcript
    base = {"has_requested": 0.97, "confirmed_readback": 0.95, "mentioned_other_group": 0.05, "enough_units": 0.9,
            "replacement_donor_required": 0.1, "will_hold": 0.8, "open_now": 0.95}
    base.update(signals)
    f = Facility(id=name, name=name, area="HSR Layout", lat=0, lon=0, phones=[Phone(number="+91")])
    return CallRecord(facility=f, phone=f.phones[0], status="completed", outcome="available", transcript=transcript,
                      units=units, eta_min=eta, groups_heard=list(heard), signals=base)


def test_all_checks_pass_confirms():
    c = call()
    gate(c, REQ, trusted=True)
    assert c.availability == "confirmed" and c.gate_reasons == []


def test_model_sure_but_transcript_disagrees_is_unconfirmed():
    c = call(transcript="assistant: Do you have O negative?\nuser: haan hai")
    gate(c, REQ, trusted=True)
    assert c.availability == "unconfirmed"
    assert any("transcript check" in r for r in c.gate_reasons)


def test_other_group_heard_blocks_confirmation():
    c = call(heard=("O+",), mentioned_other_group=0.6)
    gate(c, REQ, trusted=True)
    assert c.availability == "unconfirmed"
    assert any("O+" in r for r in c.gate_reasons)


def test_untrusted_backend_blocks_confirmation():
    c = call()
    gate(c, REQ, trusted=False)
    assert c.availability == "unconfirmed"


def test_not_reached_is_unknown_not_unavailable():
    c = call()
    c.status = "no-answer"
    gate(c, REQ, trusted=True)
    assert c.availability == "unknown"


def test_ranking_prefers_near_no_donor():
    near, far_donor = call("near", eta=8), call("far", eta=30, replacement_donor_required=0.9)
    for c in (near, far_donor):
        gate(c, REQ, trusted=True)
    assert [c.facility.name for c in rank([far_donor, near])] == ["near", "far"]


def test_plan_tops_up_from_next_bank():
    a, b = call("a", units=1), call("b", units=5)
    for c in (a, b):
        gate(c, REQ, trusted=True)
    plan = plan_units(rank([a, b]), a, needed=3)
    assert [(p.facility, p.units) for p in plan] == [("a", 1), ("b", 2)]


def test_partial_stock_with_low_open_now_is_not_closed():
    # Regression: a bank that said it has 3 of 5 units was logged as "closed" because the
    # open_now score was low. Only an explicit closed outcome means closed.
    c = call(units=3, open_now=0.19)
    c.outcome = "partial"
    gate(c, REQ, trusted=True)
    assert c.availability != "closed"


def test_explicit_closed_outcome_is_closed():
    c = call(open_now=0.95)
    c.outcome = "closed"
    gate(c, REQ, trusted=True)
    assert c.availability == "closed"
