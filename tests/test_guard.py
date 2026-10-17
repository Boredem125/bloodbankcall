from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.guard import NumberLimiter, SearchGuard, client_key


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_per_client_hourly_limit():
    clock = Clock()
    g = SearchGuard(max_per_hour=5, max_concurrent=100, clock=clock)
    for _ in range(5):
        assert g.admit("1.2.3.4") is None
        clock.t += 60
    msg = g.admit("1.2.3.4")
    assert msg and "5 per hour" in msg and "call the blood banks" in msg
    assert g.admit("5.6.7.8") is None  # other clients unaffected
    clock.t += 3600 - 5 * 60  # exactly an hour after the first search: only that one expires
    assert g.admit("1.2.3.4") is None
    assert g.admit("1.2.3.4") is not None


def test_refused_searches_do_not_count():
    clock = Clock()
    g = SearchGuard(max_per_hour=1, max_concurrent=100, clock=clock)
    assert g.admit("a") is None
    for _ in range(10):
        assert g.admit("a") is not None
    clock.t += 3601
    assert g.admit("a") is None


def test_global_concurrency_cap_and_release():
    g = SearchGuard(max_per_hour=100, max_concurrent=2, clock=Clock())
    assert g.admit("a") is None and g.admit("b") is None
    msg = g.admit("c")
    assert msg and "running right now" in msg
    g.release()
    assert g.admit("c") is None


def test_client_key_ignores_forwarded_for_unless_trusted():
    req = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"), headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
    assert client_key(req) == "10.0.0.1"
    assert client_key(req, trust_forwarded_for=True) == "9.9.9.9"


def test_number_limiter_window():
    clock = Clock()
    lim = NumberLimiter(max_per_hour=2, clock=clock)
    lim.record("+910000000101")
    lim.record("+910000000101")
    assert not lim.allow("+910000000101") and lim.allow("+910000000102")
    clock.t += 3601
    assert lim.allow("+910000000101")


def test_api_returns_429_with_helpful_message():
    from app.main import app

    with TestClient(app) as client:
        app.state.guard, app.state.guard_owner = SearchGuard(2, 100), app.state.orch
        try:
            assert client.post("/searches", json={"blood_group": "B+", "units": 1}).status_code == 200
            assert client.post("/searches", json={"blood_group": "B+", "units": 1}).status_code == 200
            r = client.post("/searches", json={"blood_group": "B+", "units": 1})
            assert r.status_code == 429 and "Too many searches" in r.json()["detail"]
            assert client.post("/searches", json={"blood_group": "X+"}).status_code == 422  # validation first
        finally:
            del app.state.guard


def test_api_caps_concurrent_searches():
    from app.main import app

    with TestClient(app) as client:
        app.state.guard, app.state.guard_owner = SearchGuard(100, 1), app.state.orch
        try:
            assert client.post("/searches", json={"units": 1}).status_code == 200  # still calling (sim speed 1)
            r = client.post("/searches", json={"units": 1})
            assert r.status_code == 429 and "running right now" in r.json()["detail"]
        finally:
            del app.state.guard


def test_guard_is_rebuilt_per_app_run():
    from app.main import app

    with TestClient(app):
        app.state.guard = SearchGuard(0, 0)  # stale guard from an older run (owner mismatch)
        app.state.guard_owner = object()
        from app.guard import get_guard

        g = get_guard(app.state)
        assert g.max_per_hour == app.state.settings.max_searches_per_client_per_hour
        assert g.max_concurrent == app.state.settings.max_concurrent_searches
