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
