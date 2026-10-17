from app.schemas import BloodRequest, Search


async def run(orch, store, **req):
    search = store.add(Search(request=BloodRequest(**req)))
    await orch.run(search.id)
    return search


def by_name(search):
    return {c.facility.name: c for c in search.calls}


async def test_recommends_nearest_confirmed_with_hold(orch, store):
    s = await run(orch, store, blood_group="O-", units=2, area="HSR Layout")
    assert s.status == "done"
    chosen = s.call(s.chosen_call_id)
    assert chosen.facility.name == "Sanjeevani 24x7 Blood Centre"
    assert chosen.availability == "confirmed" and chosen.units == 4 and chosen.staff == "Ramesh"
    assert [(p.facility, p.units) for p in s.plan] == [("Sanjeevani 24x7 Blood Centre", 2)]
    assert any("Confirm at the blood bank counter" in w for w in s.warnings)


async def test_misheard_group_is_never_confirmed(orch, store):
    # "zero positive" when O negative was requested: the gate must not say available
    s = await run(orch, store, blood_group="O-", units=2)
    red_drop = by_name(s)["Jayanagar Red Drop Blood Centre"]
    assert red_drop.availability != "confirmed"
    assert "O+" in red_drop.groups_heard


async def test_ambiguous_answer_is_unconfirmed_with_reasons(orch, store):
    s = await run(orch, store, blood_group="O-", units=2)
    hope = by_name(s)["Whitefield Hope Blood Centre"]
    assert hope.availability == "unconfirmed"
    assert hope.gate_reasons
    assert any("Whitefield Hope" in w for w in s.warnings)


async def test_switchboard_fallback_reaches_blood_bank(orch, store):
    s = await run(orch, store, blood_group="O-", units=2)
    kc = [c for c in s.calls if c.facility.id == "kc_hospital"]
    assert [c.phone.label for c in kc] == ["blood bank", "switchboard"]
    assert kc[0].availability == "unknown" and kc[1].availability == "confirmed"


async def test_split_plan_when_no_single_bank_has_enough(orch, store):
    orch.facilities = [f for f in orch.facilities if f.id in {"seva", "kc_hospital", "unity"}]
    s = await run(orch, store, blood_group="O-", units=3)
    assert s.status == "done"
    assert sum(p.units for p in s.plan) == 3
    assert len(s.plan) == 2
    assert any("split" in w for w in s.warnings)


async def test_nothing_confirmed_fails_with_leads(orch, store):
    orch.facilities = [f for f in orch.facilities if f.id in {"hope", "unity", "lifeline"}]
    s = await run(orch, store, blood_group="O-", units=2)
    assert s.status == "failed"
    assert "Whitefield Hope" in s.reason and "+910000000107" in s.reason


async def test_live_mock_answers_are_not_trusted(orch, store, monkeypatch):
    monkeypatch.setattr(orch.provider, "name", "bolna")  # pretend live with the mock decision engine
    s = await run(orch, store, blood_group="O-", units=2)
    assert all(c.availability != "confirmed" for c in s.calls)
    assert s.status == "failed"


async def test_live_mode_caps_paid_calls(orch, store, monkeypatch):
    monkeypatch.setattr(orch.provider, "name", "bolna")
    orch.s.max_live_calls_per_search = 3
    s = await run(orch, store, blood_group="O-", units=2)
    assert len(s.calls) == 3


async def test_live_calls_respect_account_concurrency(orch, store, monkeypatch):
    monkeypatch.setattr(orch.provider, "name", "bolna")
    orch.s.max_live_calls_per_search = 6
    peak, search_ref = 0, {}
    real_start = orch.provider.start

    async def counting_start(facility, phone, user_data):
        nonlocal peak
        active = [c for c in search_ref["s"].calls if c.status in ("queued", "ringing", "in-progress")]
        peak = max(peak, len(active) + 1)
        return await real_start(facility, phone, user_data)

    monkeypatch.setattr(orch.provider, "start", counting_start)
    search = store.add(Search(request=BloodRequest()))
    search_ref["s"] = search
    await orch.run(search.id)
    assert peak <= orch.s.live_concurrency


async def test_closed_and_voicemail_are_not_answers(orch, store):
    orch.facilities = [f for f in orch.facilities if f.id in {"lifeline", "voicebox", "sanjeevani"}]
    s = await run(orch, store, blood_group="O-", units=2)
    names = by_name(s)
    assert names["BTM Lifeline Blood Bank"].availability == "closed"
    assert names["Koramangala Community Blood Bank"].availability == "unknown"


async def test_no_patient_name_in_call_data(orch, store):
    sent = []
    real_start = orch.provider.start

    async def spy(facility, phone, user_data):
        sent.append(user_data)
        return await real_start(facility, phone, user_data)

    orch.provider.start = spy
    await run(orch, store, blood_group="B+", units=1, hospital="City Hospital")
    assert sent and all("patient" not in k for d in sent for k in d)
    assert sent[0]["blood_group_spoken"] == "B positive" and sent[0]["blood_group_confirm"] == "B plus"


# ----------------------------------------------------- per-number rate limit
async def test_number_is_not_called_more_than_k_times_per_hour(orch, store):
    orch.facilities = [f for f in orch.facilities if f.id == "sanjeevani"]
    orch.cache.ttl_s = -1  # no reuse: every search wants to call
    for _ in range(4):
        s = await run(orch, store, blood_group="O-", units=1)
        assert [c.phone.number for c in s.calls] == ["+910000000101"]
    s = await run(orch, store, blood_group="O-", units=1)
    assert s.calls == [] and s.status == "failed"
    assert any("skipped blood bank +910000000101, already called 4 times in the last hour" in line for line in s.log)


async def test_rate_limited_number_falls_through_to_next_number(orch, store):
    orch.facilities = [f for f in orch.facilities if f.id == "kc_hospital"]
    for _ in range(4):
        orch.limiter.record("+910000000102")
    s = await run(orch, store, blood_group="O-", units=1)
    assert [(c.phone.label, c.attempt) for c in s.calls] == [("switchboard", 1)]
    assert s.calls[0].availability == "confirmed"


# ------------------------------------------------------------ pickup learning
from app.facilities import PickupStats, call_order, phone_order  # noqa: E402


def test_call_order_without_history_is_24x7_then_nearest(orch):
    plain = call_order(orch.facilities, "HSR Layout")
    assert call_order(orch.facilities, "HSR Layout", PickupStats(), hour=3) == plain
    assert plain[0].id == "sanjeevani"


def test_call_order_prefers_numbers_that_pick_up_at_this_hour(orch):
    stats = PickupStats()
    for _ in range(6):
        stats.record("+910000000101", answered=False, hour=3)  # Sanjeevani never answers at 3am
    night = call_order(orch.facilities, "HSR Layout", stats, hour=3)
    ids = [f.id for f in night]
    assert ids.index("sanjeevani") > 0
    assert all(f.open_24x7 for f in night[: ids.index("sanjeevani") + 1])  # still ahead of every non-24x7
    assert call_order(orch.facilities, "HSR Layout", stats, hour=15)[0].id == "sanjeevani"  # other hours unaffected


def test_phone_order_needs_a_clear_margin():
    from app.facilities import load_facilities
    from app.config import Settings

    kc = next(f for f in load_facilities(Settings(_env_file=None).facilities_file) if f.id == "kc_hospital")
    stats = PickupStats()
    stats.record("+910000000102", answered=False, hour=2)
    assert [p.label for p in phone_order(kc, stats, hour=2)] == ["blood bank", "switchboard"]  # one miss is noise
    for _ in range(3):
        stats.record("+910000000102", answered=False, hour=2)
    assert [p.label for p in phone_order(kc, stats, hour=2)] == ["switchboard", "blood bank"]


async def test_search_records_pickups_and_reorders_phones(orch, store):
    orch.facilities = [f for f in orch.facilities if f.id == "kc_hospital"]
    orch.cache.ttl_s = -1
    s = await run(orch, store, blood_group="O-", units=1)
    assert [c.phone.label for c in s.calls] == ["blood bank", "switchboard"]
    assert orch.pickup.chance("+910000000102") < 0.5 < orch.pickup.chance("+910000000103")
    for _ in range(2):
        await run(orch, store, blood_group="O-", units=1)
    s = await run(orch, store, blood_group="O-", units=1)
    assert [c.phone.label for c in s.calls] == ["switchboard"]  # learned: the direct line never answers now
    assert any("trying switchboard first" in line for line in s.log)


def test_pickup_stats_persist(tmp_path):
    path = tmp_path / "pickup.json"
    stats = PickupStats(path)
    stats.record("+910000000101", answered=True, hour=4)
    stats.record("+910000000101", answered=False, hour=4)
    again = PickupStats(path)
    assert again.chance("+910000000101", hour=4) == stats.chance("+910000000101", hour=4) == 0.5
    assert again._counts["+910000000101"][4] == [1, 2]
    path.write_text("not json", encoding="utf-8")
    assert PickupStats(path).chance("+910000000101", hour=4) == 0.5  # unreadable file: start fresh
