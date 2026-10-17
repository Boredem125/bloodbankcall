from app.ranking import rank
from app.schemas import CallRecord, Vendor


def call(name, price, rating=4.5, **signals):
    base = {"got_quote": 0.95, "quoted": 0.9, "within_budget": 0.9, "hidden_charges": 0.1, "slot_fits": 0.85, "reliability": 0.85}
    base.update(signals)
    v = Vendor(id=name, name=name, category="ac_repair", area="x", rating=rating, phone="+91")
    return CallRecord(vendor=v, status="completed", price=price, signals=base)


def test_hidden_charges_lose_to_honest_quote_at_same_price():
    honest, sneaky = call("honest", 650), call("sneaky", 649, hidden_charges=0.85)
    assert [c.vendor.name for c in rank([sneaky, honest], "cheapest")] == ["honest", "sneaky"]


def test_non_quotes_are_not_viable():
    refused = call("refused", None, got_quote=0.05, quoted=0.03)
    ok = call("ok", 700)
    ranked = rank([refused, ok], "cheapest")
    assert [c.vendor.name for c in ranked] == ["ok"]
    assert refused.viable is False and refused.rank_score is None


def test_priority_changes_the_winner():
    cheap_late = call("cheap_late", 500, slot_fits=0.2)
    pricey_now = call("pricey_now", 800, slot_fits=0.95)
    assert rank([cheap_late, pricey_now], "cheapest")[0].vendor.name == "cheap_late"
    assert rank([cheap_late, pricey_now], "fastest")[0].vendor.name == "pricey_now"
