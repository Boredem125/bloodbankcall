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
