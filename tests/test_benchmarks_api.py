"""GET /api/analytics/benchmarks over the real app.

tests/test_benchmarks.py covers the arithmetic. This covers the wiring, which
is the part that breaks silently: a route that is registered but reads the
store off the wrong object, or one that answers 200 to a request with no
session.
"""
import base64
import dataclasses
import json
import secrets
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User
from agent.routers import analytics as analytics_router

_ADMIN = User(id=1, email="admin@example.com", role="admin", allowed_repos=None,
              totp_enabled=True, must_change_password=False,
              auto_approve_commands=False, require_merge_review=True)


class FakeStore:
    """One page, no offset -- enough for store_paging.all_items()."""
    def __init__(self, rows):
        self._rows = rows

    async def asearch(self, ns, limit=None, offset=None):
        if offset or ns[0] != "episodes":
            return []
        return [SimpleNamespace(namespace=ns, key=str(i), value=v)
                for i, v in enumerate(self._rows)]


def _episode(**over):
    rec = {"task_id": "t", "goal": "g", "outcome": "shipped", "review_verdict": "READY",
           "iteration_count": 0, "cost_usd": 1.0,
           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"), **over}
    return {"content": json.dumps(rec)}


@pytest.fixture
def client(monkeypatch, tmp_path):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    fake = dataclasses.replace(srv.config, auth_secret_key=key)
    monkeypatch.setattr(srv, "config", fake)
    monkeypatch.setattr(srv.app.state, "config", fake)
    # The router did `from agent.config import PROJECTS`, so it holds its own
    # reference: rebinding agent.config.PROJECTS leaves it reading the real
    # project list, and the episode count comes back multiplied by however
    # many projects this box happens to have.
    monkeypatch.setattr(analytics_router, "PROJECTS",
                        {"proj": {"live": "/nowhere", "sandbox": "/nowhere"}})
    monkeypatch.setattr(srv.app.state, "store",
                        FakeStore([_episode(), _episode(iteration_count=3)]), raising=False)
    # Point the telemetry read at an empty tmp file so the assertions do not
    # depend on whatever this box's own logs/ happens to contain.
    from agent import episode_recall
    log = tmp_path / "retrieval_events.jsonl"
    log.write_text("")
    monkeypatch.setattr(episode_recall, "LOG_PATH", log)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    # Not `with`: entering the context runs the app's lifespan, which opens the
    # real Postgres pool and starts the pollers. Every other HTTP test in this
    # suite does the same, and it is the reason app.state.config is set at app
    # creation rather than in lifespan.
    return TestClient(srv.app)


def test_the_route_answers_with_both_windows(client):
    r = client.get("/api/analytics/benchmarks")
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 14
    assert body["current"]["tasks"] == 2
    assert body["current"]["reviewed"] == 2
    assert body["current"]["first_pass"] == 1     # one shipped clean, one after 3 redos
    assert body["current"]["first_pass_rate"] == 50.0
    assert body["previous"]["tasks"] == 0
    # No previous window to compare against, so no delta is invented.
    assert body["delta"] == {}
    assert body["sample_warning"]


def test_the_window_is_a_query_parameter(client):
    assert client.get("/api/analytics/benchmarks?window_days=7").json()["window_days"] == 7


@pytest.mark.parametrize("given,expected", [("0", 1), ("-5", 1), ("9999", 90), ("abc", 14)])
def test_an_absurd_window_is_clamped_rather_than_rejected(client, given, expected):
    """The only callers are the dashboard and somebody poking at the URL;
    400ing the second one buys nothing. `abc` is FastAPI's own 422 -- included
    here so a change to that behaviour is a visible diff, not a surprise."""
    r = client.get(f"/api/analytics/benchmarks?window_days={given}")
    if given == "abc":
        assert r.status_code == 422
    else:
        assert r.json()["window_days"] == expected


def test_it_is_not_reachable_without_a_session(monkeypatch):
    """The dependency override is what makes every other test here admin;
    without it the route must refuse, not answer with the numbers."""
    monkeypatch.setattr(srv.app.state, "store", FakeStore([]), raising=False)
    assert TestClient(srv.app).get("/api/analytics/benchmarks").status_code in (401, 403)
