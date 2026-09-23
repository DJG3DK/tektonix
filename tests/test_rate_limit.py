"""audit N-1: the auth rate limiter must not be bypassable by a client-supplied
X-Forwarded-For, and its key dicts must not grow without bound."""
from types import SimpleNamespace

import pytest

import agent.rate_limit as rl


def _req(headers: dict, peer: str = "10.0.0.9"):
    return SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: headers.get(k.lower(), d)),
        client=SimpleNamespace(host=peer),
    )


@pytest.fixture(autouse=True)
def _clean():
    rl._attempts.clear()
    rl._locked_until.clear()
    rl._last_evict = 0.0
    yield
    rl._attempts.clear()
    rl._locked_until.clear()


def test_client_ip_prefers_x_real_ip_over_forwarded_for():
    # X-Real-IP is set authoritatively by nginx; a client-supplied XFF must not win.
    req = _req({"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4"})
    assert rl.client_ip(req) == "203.0.113.7"


def test_client_ip_takes_last_forwarded_hop_not_the_client_first_hop():
    # nginx appends the real peer LAST via $proxy_add_x_forwarded_for.
    req = _req({"x-forwarded-for": "1.2.3.4, 203.0.113.7"})
    assert rl.client_ip(req) == "203.0.113.7"


def test_rotating_forwarded_for_first_hop_does_not_bypass_the_limit():
    # Same real peer (X-Real-IP), attacker rotates the spoofable XFF first hop.
    got_429 = 0
    for i in range(20):
        req = _req({"x-real-ip": "203.0.113.7", "x-forwarded-for": f"9.9.9.{i}"})
        try:
            rl.check_rate_limit(req, "login")
        except Exception as e:  # HTTPException(429)
            if getattr(e, "status_code", None) == 429:
                got_429 += 1
    assert got_429 > 0, "rotating X-Forwarded-For bypassed the limiter"


def test_eviction_bounds_the_attempts_dict(monkeypatch):
    # Distinct real peers over time should not grow the dict without bound.
    t = [1000.0]
    monkeypatch.setattr(rl.time, "time", lambda: t[0])
    for i in range(50):
        req = _req({"x-real-ip": f"203.0.113.{i}"})
        rl.check_rate_limit(req, "login")
        t[0] += 1
    before = len(rl._attempts)
    # jump far past every window + the evict interval, then one more call sweeps
    t[0] += rl._EVICT_INTERVAL + 10_000
    rl.check_rate_limit(_req({"x-real-ip": "203.0.113.250"}), "login")
    assert len(rl._attempts) < before, "stale keys were not evicted"


def test_a_successful_2fa_clears_the_verify_window(monkeypatch):
    """Login already cleared its window on success. verify-2fa did not, so a
    few typos before a good code could lock a legitimate second factor."""
    from fastapi.testclient import TestClient

    import agent.server as srv

    async def pending(pool, token):
        return {"user_id": 1}

    async def totp_ok(pool, config, uid, code):
        return True

    async def session(pool, uid):
        return "sess"

    async def user_row(pool, uid):
        return {"id": 1, "email": "a@b.co", "role": "admin", "allowed_repos": None,
                "totp_enabled": True, "must_change_password": False,
                "auto_approve_commands": False, "require_merge_review": True}

    monkeypatch.setattr(srv.auth, "resolve_pending_2fa", pending)
    monkeypatch.setattr(srv.auth, "verify_totp_or_recovery", totp_ok)
    monkeypatch.setattr(srv.auth, "create_session", session)
    monkeypatch.setattr(srv.auth, "get_user_by_id", user_row)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)

    prior = _req({"x-real-ip": "203.0.113.7"})
    rl.check_rate_limit(prior, "verify-2fa")
    rl.check_rate_limit(prior, "verify-2fa")
    assert ("203.0.113.7", "verify-2fa") in rl._attempts

    res = TestClient(srv.app).post(
        "/api/auth/2fa/verify",
        json={"temp_token": "t", "code": "123456"},
        headers={"X-Real-IP": "203.0.113.7"},
    )
    assert res.status_code == 200, res.text
    assert ("203.0.113.7", "verify-2fa") not in rl._attempts
    assert ("203.0.113.7", "verify-2fa") not in rl._locked_until


def test_an_internal_failure_lets_the_request_through_and_says_so(monkeypatch, caplog):
    """Fail open, never silently: a limiter that quietly stopped limiting would
    look exactly like one that works."""
    def boom(request):
        raise RuntimeError("limiter bug")

    monkeypatch.setattr(rl, "client_ip", boom)
    with caplog.at_level("ERROR", logger="tektonix"):
        rl.check_rate_limit(_req({}), "login")          # does not raise
    assert "request allowed through unlimited" in caplog.text


def test_the_single_process_assumption_is_written_where_it_would_break():
    assert "SINGLE PROCESS IS AN ASSUMPTION" in rl.__doc__
