"""Adversarial transcripts for the parser and the safety gate.

Rule under test: unless the staff clearly confirmed the requested group, a call never ends
"confirmed". Each must-not-confirm case runs twice: through the mock decision model, and with a
fooled model that answers every question perfectly, so the deterministic transcript checks in
the gate must block it alone.
"""
import json

import pytest

from app.config import Settings
from app.decision.base import choice, noul
from app.decision.mock import MockEngine
from app.decision.questions import call_questions, call_state
from app.orchestrator import SIGNALS
from app.parsing import (
    ambiguous_groups,
    parse_fee,
    parse_groups,
    parse_staff,
    parse_units,
    readback_confirmed,
    vendor_groups,
    words_to_digits,
)
from app.ranking import gate
from app.schemas import COMPONENTS, GROUP_CONFIRM, GROUP_SPOKEN, BloodRequest, CallRecord, Facility, Phone

PERFECT = {"has_requested": 0.99, "confirmed_readback": 0.99, "mentioned_other_group": 0.01, "enough_units": 0.95,
           "replacement_donor_required": 0.05, "will_hold": 0.9, "open_now": 0.99}


def ask(group="O-"):
    return (f"assistant: Do you have {GROUP_SPOKEN[group]}, that is {GROUP_CONFIRM[group]}, packed red cells "
            "available right now? We need 2 units.")


def rb(group="O-", units=3):
    return f"assistant: Confirming: {GROUP_SPOKEN[group]}, that is {GROUP_CONFIRM[group]}, {units} units of packed red cells, correct?"


async def assess(lines, group="O-", units=2, fooled=False):
    """Same steps as Orchestrator._assess. fooled=True swaps in a model that says yes to everything."""
    t = "\n".join(lines)
    req = BloodRequest(blood_group=group, units=units)
    f = Facility(id="x", name="x", area="HSR Layout", lat=0, lon=0, phones=[Phone(number="+91")])
    c = CallRecord(facility=f, phone=f.phones[0], status="completed", transcript=t)
    c.units, c.fee, c.staff = parse_units(t), parse_fee(t), parse_staff(t)
    c.groups_heard = vendor_groups(t)
    if fooled:
        c.outcome, c.signals = "available", dict(PERFECT)
    else:
        a = await MockEngine().decide(call_state(req, c), call_questions(req))
        c.outcome, _ = choice(a, "outcome")
        c.signals = {k: noul(a, k) for k in SIGNALS}
    gate(c, req, trusted=True)
    return c


# (id, group, lines, expected availability with the mock model)
NEVER_CONFIRM = [
    ("hinglish_negative_nahi_positive_hai", "O-",
     [ask(), "user: O negative nahi hai, positive hai, 3 units", rb(), "user: haan"], "not_available"),
    ("yes_sorry_no_finished", "O-",
     [ask(), "user: yes 3 units", rb(), "user: yes... sorry no, finished"], "not_available"),
    ("yes_then_retracted_next_turn", "O-",
     [ask(), "user: yes O negative, 3 units", rb(), "user: yes",
      "assistant: Thank you, the family is coming.", "user: wait, no, it is finished"], "not_available"),
    ("staff_corrects_readback_group", "O-",
     [ask(), "user: yes O negative 3 units", rb(), "user: yes but it is O positive actually"], "unconfirmed"),
    ("staff_corrects_readback_units", "O-",
     [ask(), "user: yes O negative is there", rb(units=3), "user: no, only 2 units"], "unconfirmed"),
    ("letter_o_without_sign", "O-", [ask(), "user: O is there, 3 units", rb(), "user: yes"], "unconfirmed"),
    ("haan_o_hai_without_sign", "O-", [ask(), "user: haan O hai, 3 units", rb(), "user: haan ji"], "unconfirmed"),
    ("stt_b_heard_as_d", "B-", [ask("B-"), "user: D negative is there, 3 units", rb("B-"), "user: yes"], "unconfirmed"),
    ("stt_ab_split_for_b_request", "B-",
     [ask("B-"), "user: A B negative hai, 3 units", rb("B-"), "user: yes correct"], "not_available"),
    ("ab_said_for_a_request", "A-", [ask("A-"), "user: AB negative is there", rb("A-"), "user: yes"], "not_available"),
    ("zero_positive_for_o_negative", "O-",
     [ask(), "user: zero positive is there, 3 units", rb(), "user: yes"], "not_available"),
    ("hindi_do_unit_vs_readback_3", "O-", [ask(), "user: haan do unit hai", rb(units=3), "user: haan"], "unconfirmed"),
    ("hindi_ek_bhi_nahi", "O-", [ask(), "user: O negative ek bhi nahi hai", rb(), "user: haan"], "not_available"),
    ("kannada_illa", "O-", [ask(), "user: O negative illa sir", rb(), "user: ok"], "not_available"),
    ("zero_units", "O-", [ask(), "user: O negative zero units right now", rb(), "user: yes"], "not_available"),
    ("readback_units_differ_from_stock", "O-",
     [ask(), "user: yes O negative, only 1 unit", rb(units=3), "user: yes"], "unconfirmed"),
    ("ivr_hold_text_after_readback", "O-",
     [ask(), "user: please hold, transferring", rb(), "user: your call is important to us, please stay on the line"],
     "not_available"),
    ("transfer_reception_yes_bank_no", "O-",
     [ask(), "user: reception here, yes O negative should be there, transferring",
      "user: blood bank, sorry O negative is not available"], "not_available"),
    ("agent_question_echoed_as_reply", "O-", [ask(), "user: O negative 3 units", rb(), "user: correct?"], "unconfirmed"),
    ("agent_readback_echoed_as_user", "O-",
     [ask(), "user: O negative 3 units", rb(), "user: Confirming O negative that is O minus 3 units correct"],
     "unconfirmed"),
    ("opening_question_says_confirm", "O-",
     ["assistant: Can you confirm whether you have O negative, that is O minus? We need 2 units.", "user: yes"],
     "unconfirmed"),
    ("agent_readback_names_two_groups", "O-",
     [ask(), "user: yes 3 units", "assistant: Confirming: O negative, that is O plus, 3 units, correct?", "user: yes"],
     "unconfirmed"),
    ("second_readback_other_group", "O-",
     [ask(), "user: O negative 3 units", rb(), "user: yes", rb("O+"), "user: yes"], "unconfirmed"),
    ("yes_to_wrong_group_readback", "O-",
     [ask(), "user: O positive is there 3 units", rb("O+"), "user: yes"], "not_available"),
    ("yeah_nope_gone", "O-", [ask(), "user: O negative 3 units", rb(), "user: yeah, nope, actually gone"], "not_available"),
    ("yes_no_problem_but_expired", "O-",
     [ask(), "user: O negative 3 units", rb(), "user: yes no problem, but expired"], "unconfirmed"),
    ("yes_let_me_check", "O-", [ask(), "user: O negative 3 units", rb(), "user: yes, let me check again"], "unconfirmed"),
    ("ok_sorry_not_sure", "O-", [ask(), "user: yes O negative 3", rb(), "user: ok sorry not sure"], "unconfirmed"),
    ("haan_nahi_b_negative", "O-",
     [ask(), "user: O negative 3 units", rb(), "user: haan... nahi nahi, B negative"], "unconfirmed"),
    ("ji_nahi", "O-", [ask(), "user: O negative 3 units", rb(), "user: ji nahi"], "unconfirmed"),
    ("garbled_noise", "O-", [ask(), "user: [inaudible] ... kshhh", rb(), "user: [noise]"], "not_available"),
    ("right_now_no_maybe_evening", "O-",
     [ask(), "user: right now we don't have, maybe evening", rb(), "user: ok"], "not_available"),
]


@pytest.mark.parametrize("cid,group,lines,expected", NEVER_CONFIRM, ids=[c[0] for c in NEVER_CONFIRM])
async def test_never_confirmed_with_mock(cid, group, lines, expected):
    c = await assess(lines, group)
    assert c.availability == expected, c.gate_reasons


@pytest.mark.parametrize("cid,group,lines,expected", NEVER_CONFIRM, ids=[c[0] for c in NEVER_CONFIRM])
async def test_never_confirmed_even_if_model_fooled(cid, group, lines, expected):
    c = await assess(lines, group, fooled=True)
    assert c.availability == "unconfirmed" and c.gate_reasons


async def test_empty_transcript_is_unknown():
    assert (await assess([""])).availability == "unknown"
    assert (await assess([""], fooled=True)).availability == "unconfirmed"


# Clean confirmations must still go through, or the product is useless.
CONFIRM = [
    ("plain", "O-", [ask(), "user: yes O negative is there, 3 units", rb(), "user: yes correct"]),
    ("zero_for_o", "O-", [ask(), "user: zero negative 3 units hai", rb(), "user: yes correct"]),
    ("oh_for_o", "O-", [ask(), "user: oh negative is available, three units", rb(), "user: haan ji, sahi hai"]),
    ("hindi_teen_unit", "O-", [ask(), "user: O negative teen unit hai", rb(), "user: haan bilkul"]),
    ("stt_duplication", "O-", [ask(), "user: yes yes O negative O negative 3 units 3 units", rb(), "user: yes yes correct correct"]),
    ("ab_split_for_ab_request", "AB-", [ask("AB-"), "user: A B negative hai, 3 units", rb("AB-"), "user: yes correct"]),
    ("reply_repeats_group_and_units", "O-", [ask(), "user: O minus, 3 units", rb(), "user: yes, O negative, 3 units, correct"]),
    ("reply_split_over_lines_with_name", "O-",
     [ask(), "user: O negative 3 units", rb(), "user: yes", "user: correct. This is Ramesh."]),
]


@pytest.mark.parametrize("cid,group,lines", CONFIRM, ids=[c[0] for c in CONFIRM])
async def test_clean_confirmations_still_confirm(cid, group, lines):
    c = await assess(lines, group)
    assert c.availability == "confirmed", c.gate_reasons


# ---------------------------------------------------------------- parser details
@pytest.mark.parametrize(
    "spoken,groups",
    [
        ("O negative nahi hai, positive hai", ["O-", "O+"]),
        ("A B negative", ["AB-"]),
        ("zero minus", ["O-"]),
        ("O -ve", ["O-"]),
        ("we have two units plus one tomorrow", []),  # "plus" alone is not a sign
    ],
)
def test_groups(spoken, groups):
    assert parse_groups(spoken) == groups


@pytest.mark.parametrize(
    "text,flagged",
    [
        ("haan O hai", True), ("B group is there", True), ("D negative", True), ("positive hai", True),
        ("negative hai", False),  # same sign as the request, like "haan hai"
        ("This is a blood bank", False), ("ab hai", False),  # English article, Hindi "now"
        ("O negative hai", False),
    ],
)
def test_ambiguous_mentions(text, flagged):
    assert bool(ambiguous_groups(text, "O-")) is flagged


def test_hindi_numbers_and_units():
    assert words_to_digits("teen unit, do bag, char bottle") == "3 unit, 2 bag, 4 bottle"
    assert words_to_digits("ek bhi nahi") == "ek bhi nahi"
    assert parse_units("user: haan do unit hai") == 2


def test_units_not_taken_from_times_fees_or_phone_numbers():
    t = "user: come by 5 pm, we keep it 2 hours, fee 1550 rupees, call 98765 43210"
    assert parse_units(t) is None


def test_fee_ignores_phone_numbers_bag_size_and_counter():
    t = "user: call 98765 43210 or 080 2345 6789, fee 1550 per unit, 350 ml bag, counter 4"
    assert parse_fee(t) == 1550
    assert parse_fee("user: call 98765 43210") is None


def test_readback_reply_must_be_plain_yes():
    base = "\n".join([ask(), "user: O negative 3 units", rb()])
    assert readback_confirmed(base + "\nuser: yes correct, no problem", "O-") is True
    assert readback_confirmed(base + "\nuser: yes, 2 units", "O-") is False
    assert readback_confirmed(base + "\nuser: yes it is finished", "O-") is False


# ---------------------------------------------------------------- side signals
async def test_hold_refused_variants():
    for reply in ("no hold, sorry", "we can not hold", "hold nahi kar sakte", "we won't keep it aside"):
        lines = [ask(), "user: yes O negative 3 units", "assistant: Can you keep them aside for one hour?",
                 f"user: {reply}", rb(), "user: yes correct"]
        c = await assess(lines)
        assert c.signals["will_hold"] < 0.5, reply


@pytest.mark.parametrize(
    "reply,needed",
    [
        ("you must bring a replacement donor", True),
        ("exchange donor dena padega", True),
        ("one family member has to donate", True),
        ("no need of replacement", False),
        ("donor not required", False),
        ("no replacement needed", False),
    ],
)
async def test_replacement_donor_variants(reply, needed):
    c = await assess([ask(), "user: yes O negative 3 units", "assistant: Is a replacement donor required?", f"user: {reply}"])
    assert (c.signals["replacement_donor_required"] >= 0.5) is needed


@pytest.mark.parametrize(
    "reply",
    ["blood bank is closed now", "bandh hai, kal subah aana", "opens at 8 am", "come tomorrow morning",
     "it is after hours, timing is over"],
)
async def test_closed_variants(reply):
    c = await assess([ask(), f"user: {reply}", rb(), "user: yes"])
    assert c.availability == "closed"


async def test_never_closed_is_not_closed():
    c = await assess([ask(), "user: we are never closed, O negative is there, 3 units", rb(), "user: yes correct"])
    assert c.availability == "confirmed"


# ---------------------------------------------------------------- new sim personas
def _render(persona, group="O-", stock=2):
    scripts = json.loads(Settings(_env_file=None).sim_scripts_file.read_text(encoding="utf-8"))
    fill = {"facility_name": "Test", "blood_group_spoken": GROUP_SPOKEN[group], "blood_group_confirm": GROUP_CONFIRM[group],
            "component": COMPONENTS["prbc"], "units": 2, "stock": stock, "fee": 1500, "staff": "Ravi"}
    return [ln.format_map(fill) for ln in scripts[persona]["lines"]]


@pytest.mark.parametrize("persona", ["hinglish_flip", "retracted"])
async def test_tricky_personas_never_confirm(persona):
    for fooled in (False, True):
        assert (await assess(_render(persona), fooled=fooled)).availability != "confirmed"


async def test_has_stock_persona_still_confirms():
    assert (await assess(_render("has_stock"))).availability == "confirmed"
