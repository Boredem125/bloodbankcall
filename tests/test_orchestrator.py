import asyncio

from app.schemas import Errand, ErrandRequest


async def run(orch, store, **req):
    errand = store.add(Errand(request=ErrandRequest(task=req.pop("task", "AC service tomorrow, cooling is weak"), **req)))
    await orch.run(errand.id)
    return errand


async def test_ac_errand_recommends_honest_cheapest(orch, store):
    e = await run(orch, store, budget=800)
    assert e.category == "ac_repair"
    assert e.status == "awaiting_approval"
    chosen = e.call(e.chosen_call_id)
    assert chosen.vendor.name == "CoolCare AC Services"
    assert chosen.price == 650
    # the hidden-fee and no-slot vendors were understood as such
    by_name = {c.vendor.name: c for c in e.calls}
    assert by_name["Sri Sai Cooling Works"].signals["hidden_charges"] > 0.5
    assert by_name["Raju AC Repair"].outcome == "no_slot" and not by_name["Raju AC Repair"].viable
    assert "Sri Sai Cooling Works (hidden charges)" in e.reason
    stages = [d.stage for d in e.decisions]
    assert stages[0] == "intake" and stages[-1] == "final" and stages.count("call") == 5


async def test_approve_places_booking_call(orch, store):
    e = await run(orch, store)
    await orch.approve(e.id)
    for _ in range(200):
        if e.status in ("booked", "failed"):
            break
        await asyncio.sleep(0.02)
    assert e.status == "booked"
    assert e.calls[-1].purpose == "book" and "confirmed" in e.calls[-1].transcript.lower()


async def test_auto_book_under_limit(orch, store):
    e = await run(orch, store, auto_book_limit=700)
    assert e.status == "booked"
    assert any("Auto-booking" in line for line in e.log)


async def test_escalates_when_too_few_quotes(orch, store):
    orch.s.initial_calls = 1  # first batch: Frosty (over budget but viable) -> needs one more
    e = await run(orch, store)
    assert len([c for c in e.calls if c.purpose == "quote"]) >= 2
    assert any("calling" in line and "more" in line for line in e.log)


async def test_live_mode_caps_calls(orch, store, monkeypatch):
    monkeypatch.setattr(orch.provider, "name", "bolna")  # pretend live: cap applies
    orch.s.max_live_calls_per_errand = 3
    orch.s.poll_interval_s = 0.02
    e = await run(orch, store)
    assert len(e.calls) == 3


async def test_live_calls_respect_account_concurrency(settings, store):
    from app.decision import build_engine
    from app.orchestrator import Orchestrator
    from app.providers import SimProvider
    from app.vendors import load_vendors

    settings.live_concurrency = 2
    settings.poll_interval_s = 0.02
    provider = SimProvider(settings)
    provider.name = "bolna"  # pretend live
    orch = Orchestrator(settings, provider, build_engine(settings), store, load_vendors(settings.vendors_file))
    errand = store.add(Errand(request=ErrandRequest(task="AC service tomorrow")))
    peak = 0
    real_start = provider.start

    async def counting_start(vendor, user_data):
        nonlocal peak
        active = [c for c in errand.calls if c.status in ("queued", "ringing", "in-progress")]
        peak = max(peak, len(active) + 1)
        return await real_start(vendor, user_data)

    provider.start = counting_start
    await orch.run(errand.id)
    assert len(errand.calls) == 3
    assert peak <= 2


async def test_unknown_category_fails_cleanly(orch, store):
    orch.vendors = []
    e = await run(orch, store)
    assert e.status == "failed" and "No businesses" in e.reason
