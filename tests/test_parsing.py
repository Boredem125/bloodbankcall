import pytest

from app.parsing import parse_price, parse_slot, words_to_digits

HIDDEN = """assistant: Can you come tomorrow evening? What will it cost?
user: Service is only 399 rupees, sir. Slot at 5 pm.
assistant: Any visiting charge?
user: Visiting charge 250 alag se.
assistant: So the minimum total would be how much?
user: Minimum 649 rupees total, parts extra."""


def test_last_amount_said_by_business_is_the_total():
    assert parse_price(HIDDEN) == 649


def test_phone_numbers_and_times_are_not_prices():
    t = "user: Call me on 9876543210 at 6:30 pm, price is 1,200 rupees."
    assert parse_price(t) == 1200


def test_falls_back_to_agent_confirmation():
    t = "user: haan theek hai\nassistant: So 750 rupees total, confirmed?"
    assert parse_price(t) == 750


def test_extraction_wins_over_transcript():
    assert parse_price(HIDDEN, {"quoted_price": "Rs. 700"}) == 700


def test_no_price():
    assert parse_price("user: please call back later") is None


REAL_CALL = """assistant: Namaste! Am I speaking with Test AC Services?
user:  yes that's it sir just tell me
assistant:  Hi, we need AC service for weak cooling in HSR Layout tomorrow evening. Could you do it tomorrow evening, and what time would be available?
user:  yes we can do it tomorrow at six pm
assistant:  Great, what would be the total price in rupees? Also, is there any visiting charge, parts, GST, or any other extra cost?
user:  we usually take fifty rupees and have a visiting charge of two hundred
assistant:  Can you do the total for 800 rupees? So the total is 250 rupees at 6 pm, correct?
user:  yeah work it's okay
assistant:  Thank you, I'll confirm shortly."""


def test_real_bolna_call_uses_confirmed_readback_total():
    # first live call, 2026-10-17: base 50 + visiting 200, agent read back 250, vendor agreed
    assert parse_price(REAL_CALL) == 250
    assert parse_slot(REAL_CALL) == "tomorrow 6 pm"


REAL_CALL_2 = """assistant: Namaste! Am I speaking with Test AC Services?
user:  yeah who is this
assistant:  I'm calling for Ananya about AC service in HSR Layout tomorrow evening; can you do it, and at what time?
user:  yeah it was five pm work
assistant:  What is the price in rupees?
user:  we usually take seven hundred rupees
assistant:  Is there any visiting charge, parts, GST or anything extra on top of that?
user:  yeah one fifty rupees as well
assistant:  So the total is 850 rupees at 5 pm, correct?
user:  yeah"""


def test_real_bolna_call_2_base_plus_extra():
    # second live call with the one-question-per-turn prompt: 700 + "one fifty"
    assert parse_price(REAL_CALL_2) == 850
    assert parse_slot(REAL_CALL_2) == "5 pm"


def test_vendor_correction_after_readback_wins():
    t = "assistant: So the total is 600 rupees, correct?\nuser: no no, seven fifty total"
    assert parse_price(t) == 750


@pytest.mark.parametrize(
    "spoken,digits",
    [
        ("visiting charge of two hundred", "visiting charge of 200"),
        ("total six fifty only", "total 650 only"),
        ("one thousand two hundred rupees", "1200 rupees"),
        ("two hundred and fifty", "250"),
        ("do sau pachaas", "250"),
        ("saat sau rupaye", "700 rupaye"),
        ("fifteen hundred", "1500"),
        ("we can do it at six pm", "we can do it at 6 pm"),
        ("saath mein ek din", "saath mein ek din"),
    ],
)
def test_words_to_digits(spoken, digits):
    assert words_to_digits(spoken) == digits


def test_times_and_durations_are_not_prices():
    assert parse_price("user: come at ten am, takes 45 minutes") is None


def test_slot():
    assert parse_slot(HIDDEN) == "5 pm"
    assert parse_slot("user: tomorrow 9 am works", {"available_slot": "Sunday 10 am"}) == "Sunday 10 am"


# ---------------------------------------------------------------- blood bank calls
from app.parsing import parse_fee, parse_groups, parse_staff, parse_units, readback_confirmed  # noqa: E402

BB = """assistant: Do you have O negative, that is O minus, packed red cells available right now? We need 2 units.
user: yes this is Ramesh, we have three units, fee is 1550 rupees per unit
assistant: Can you keep them aside for one hour?
user: yes we can hold for one hour
assistant: Confirming: O negative, that is O minus, 3 units, kept aside, correct?
user: yes correct, no problem"""


@pytest.mark.parametrize(
    "spoken,groups",
    [
        ("yes we have O negative", ["O-"]),
        ("zero negative is there", ["O-"]),  # STT hears "O" as zero
        ("oh minus two units", ["O-"]),
        ("B +ve and AB-", ["B+", "AB-"]),
        ("AB positive", ["AB+"]),
    ],
)
def test_parse_groups(spoken, groups):
    assert parse_groups(spoken) == groups


def test_blood_call_fields():
    assert parse_units(BB) == 3
    assert parse_fee(BB) == 1550
    assert parse_staff(BB) == "Ramesh"
    assert readback_confirmed(BB, "O-") is True


def test_need_is_not_mistaken_for_stock():
    t = "assistant: Do you have O negative? We need 2 units.\nuser: sorry, not available"
    assert parse_units(t) is None


def test_readback_rejected():
    t = "assistant: Confirming: O negative, that is O minus, correct?\nuser: no, I said O positive"
    assert readback_confirmed(t, "O-") is False


def test_no_readback():
    assert readback_confirmed("user: yes we have", "O-") is None


# Third live call (2026-10-17), first one with the blood bank agent: STT tail noise in the yes,
# lowercase staff name, partial stock.
REAL_BLOOD_CALL = """assistant: Namaste, I am an AI assistant calling for a patient's family; this call may be recorded. Is this the blood bank?
user:  yes
assistant:  Do you have O negative, that is O minus, packed red cells (PRBC) available right now? We need 2 units.
user:  yeah it's available
assistant:  How many units do you have right now?
user:  we have only one unit
assistant:  Do you require a replacement donor, and what is the processing fee per unit?
user:  yeah we do require the processing fee purchasing fee is like five hundred rupees
assistant:  Can you keep the 1 unit aside for one hour if the family comes now?
user:  yeah so
assistant:  What is your name?
user:  my name is arian
assistant: Confirming: O negative, that is O minus, 1 unit of packed red cells (PRBC), kept aside for one hour, correct?
user:  sure perfect  work
assistant:  Thank you; the family will bring the doctor's requisition form and the patient's blood sample.
user:  yeah
assistant: Thank
user:  thank you"""


def test_real_blood_call_parses():
    assert readback_confirmed(REAL_BLOOD_CALL, "O-") is True
    assert parse_units(REAL_BLOOD_CALL) == 1
    assert parse_fee(REAL_BLOOD_CALL) == 500
    assert parse_staff(REAL_BLOOD_CALL) == "Arian"


@pytest.mark.parametrize("reply", ["sure perfect work", "yes that works", "haan theek hai, thank you"])
def test_noisy_yes_still_counts(reply):
    t = f"assistant: Confirming: O negative, that is O minus, 2 units, correct?\nuser: {reply}"
    assert readback_confirmed(t, "O-") is True


@pytest.mark.parametrize("reply", ["this is wrong", "sure, no wait", "work it out, not sure", "yes but finished now"])
def test_noisy_no_still_fails(reply):
    t = f"assistant: Confirming: O negative, that is O minus, 2 units, correct?\nuser: {reply}"
    assert readback_confirmed(t, "O-") is not True


def test_lowercase_name_not_a_word():
    assert parse_staff("user: this is the blood bank") is None
    assert parse_staff("user: this is correct") is None
