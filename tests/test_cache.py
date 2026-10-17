import time

from app.cache import FreshnessCache, age_text
from app.schemas import BloodRequest, CallRecord, Facility, Phone, Search

FAC = Facility(id="f1", name="F1", area="HSR Layout", lat=12.9, lon=77.6, phones=[Phone(number="+910000000999")])


def _call(availability="confirmed", units=4):
    return CallRecord(facility=FAC, phone=FAC.phones[0], status="completed", availability=availability,
                      units=units, signals={"enough_units": 1.0})


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


async def run(orch, store, **req):
    search = store.add(Search(request=BloodRequest(**req)))
    await orch.run(search.id)
    return search


def count_starts(orch, monkeypatch):
    dialed = []
    real_start = orch.provider.start

    async def spy(facility, phone, user_data):
        dialed.append(phone.number)
        return await real_start(facility, phone, user_data)

    monkeypatch.setattr(orch.provider, "start", spy)
    return dialed


# ------------------------------------------------------------------ unit
def test_fresh_answer_is_reused_as_a_marked_copy():
    clock = Clock()
    cache = FreshnessCache(ttl_s=20 * 60, clock=clock)
    req = BloodRequest(blood_group="O-", units=2)
    original = _call()
    cache.put(original, req)
    clock.t += 12 * 60
    call, age = cache.get("f1", req)
    assert age == 12 * 60 and age_text(age) == "12 min ago"
    assert call.id != original.id and call.reused_at == clock.t - 12 * 60
    assert call.availability == "confirmed" and call.units == 4
    assert original.reused_at is None  # the cached original is untouched


def test_stale_confirmation_is_never_reused():
    clock = Clock()
    cache = FreshnessCache(ttl_s=20 * 60, clock=clock)
    req = BloodRequest()
    cache.put(_call(), req)
    clock.t += 20 * 60 + 1
    assert cache.get("f1", req) is None
    clock.t -= 10 * 60  # expired entries are gone for good, even if the clock looks younger later
    assert cache.get("f1", req) is None


def test_only_definite_answers_are_cached_per_group_and_component():
    cache = FreshnessCache(ttl_s=600, clock=Clock())
    req = BloodRequest(blood_group="O-", component="prbc")
    for availability in ("unconfirmed", "unknown", "closed"):
        cache.put(_call(availability), req)
        assert cache.get("f1", req) is None
    cache.put(_call("not_available", units=None), req)
    assert cache.get("f1", req)[0].availability == "not_available"
    assert cache.get("f1", BloodRequest(blood_group="O+", component="prbc")) is None
    assert cache.get("f1", BloodRequest(blood_group="O-", component="platelets")) is None


def test_reused_answer_is_not_cached_again():
    clock = Clock()
    cache = FreshnessCache(ttl_s=600, clock=clock)
    req = BloodRequest()
    cache.put(_call(), req)
    clock.t += 500
    reused, _ = cache.get("f1", req)
    cache.put(reused, req)  # must not refresh the timestamp
    clock.t += 200
    assert cache.get("f1", req) is None


def test_bigger_request_needs_a_stated_unit_count():
    cache = FreshnessCache(ttl_s=600, clock=Clock())
    cache.put(_call(units=None), BloodRequest(units=1))
    assert cache.get("f1", BloodRequest(units=3)) is None
    cache.put(_call(units=2), BloodRequest(units=1))
    call, _ = cache.get("f1", BloodRequest(units=3))
    assert call.signals["enough_units"] == 0.0  # recomputed for the new request


# ------------------------------------------------------------- pipeline
async def test_second_search_reuses_confirmation_without_calling(orch, store, monkeypatch):
    await run(orch, store, blood_group="O-", units=2)
    dialed = count_starts(orch, monkeypatch)
    s = await run(orch, store, blood_group="O-", units=2)
    sanj = [c for c in s.calls if c.facility.id == "sanjeevani"]
    assert len(sanj) == 1 and sanj[0].reused_at is not None and sanj[0].availability == "confirmed"
    assert "+910000000101" not in dialed
    assert any("Sanjeevani 24x7 Blood Centre: reused confirmation from" in line for line in s.log)
    assert s.call(s.chosen_call_id).facility.id == "sanjeevani"
    assert any("earlier search" in w for w in s.warnings)


async def test_not_available_is_reused_but_unconfirmed_is_called_again(orch, store, monkeypatch):
    orch.facilities = [f for f in orch.facilities if f.id in {"unity", "hope", "sanjeevani"}]
    await run(orch, store, blood_group="O-", units=2)
    dialed = count_starts(orch, monkeypatch)
    s = await run(orch, store, blood_group="O-", units=2)
    unity = next(c for c in s.calls if c.facility.id == "unity")
    assert unity.reused_at is not None and unity.availability == "not_available"
    assert any("reused 'not available'" in line for line in s.log)
    assert "+910000000107" in dialed  # Whitefield Hope was only unconfirmed: never reused
    assert "+910000000106" not in dialed


async def test_expired_confirmation_is_called_again(orch, store, monkeypatch):
    await run(orch, store, blood_group="O-", units=2)
    orch.cache.clock = lambda: time.time() + 21 * 60
    dialed = count_starts(orch, monkeypatch)
    s = await run(orch, store, blood_group="O-", units=2)
    assert "+910000000101" in dialed
    assert all(c.reused_at is None for c in s.calls)


async def test_other_blood_group_is_called_fresh(orch, store, monkeypatch):
    await run(orch, store, blood_group="O-", units=2)
    dialed = count_starts(orch, monkeypatch)
    s = await run(orch, store, blood_group="B+", units=2)
    assert "+910000000101" in dialed and all(c.reused_at is None for c in s.calls)


async def test_untrusted_live_answers_are_not_cached(orch, store, monkeypatch):
    monkeypatch.setattr(orch.provider, "name", "bolna")  # live with mock decisions: nothing trusted
    orch.s.max_live_calls_per_search = 6
    await run(orch, store, blood_group="O-", units=2)
    assert orch.cache._entries == {}
