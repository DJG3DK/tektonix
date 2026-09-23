"""Regression test for the API balance vanishing from the sidebar
(2026-08-24): the frontend used to call /_review/api/router/balance
directly, and nginx still gates that path behind the OLD shared
reverse-proxy login -- one this app's own users no longer
necessarily have now that /v2/ dropped that redundant gate in favor of
agent/auth.py's own login. GET /api/router-balance proxies through this
app's own backend (and its own auth) instead."""

from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User

_FAKE_USER = User(id=1, email="test@example.com", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    last_url = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        _FakeAsyncClient.last_url = url
        _FakeAsyncClient.last_headers = headers or {}
        return _FakeResponse({"totalCredits": 165, "totalUsage": 143.4, "remaining": 21.6})


def test_router_balance_proxies_the_review_service_through_this_apps_own_auth(monkeypatch):
    from agent.tools import review_gate
    # The headers are review_gate's, computed from REVIEW_CONTROL_SECRET at
    # import. Set explicitly: CI runs with no .env, where they are empty, and
    # this asserts the route passes them on -- not what this box's secret is.
    monkeypatch.setattr(review_gate, "_CONTROL_HEADERS", {"X-Review-Secret": "the-secret"})
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _FAKE_USER)
    monkeypatch.setattr(srv.httpx, "AsyncClient", _FakeAsyncClient)
    client = TestClient(srv.app)

    res = client.get("/api/router-balance")

    assert res.status_code == 200
    assert res.json() == {"totalCredits": 165, "totalUsage": 143.4, "remaining": 21.6}
    from agent.tools.review_gate import REVIEW_SERVICE_HOST, REVIEW_SERVICE_PORT
    # review_gate's address, not a copy of it (2026-09-23 review, 8.1), and the
    # control secret, which the review service's reads now require (3.1).
    assert _FakeAsyncClient.last_url == f"http://{REVIEW_SERVICE_HOST}:{REVIEW_SERVICE_PORT}/api/router/balance"
    assert _FakeAsyncClient.last_headers == {"X-Review-Secret": "the-secret"}


def test_router_balance_requires_login(monkeypatch):
    srv.app.dependency_overrides.pop(srv.require_full_auth, None)
    monkeypatch.setattr(srv.app.state, "auth_pool", None, raising=False)
    client = TestClient(srv.app)

    res = client.get("/api/router-balance")

    assert res.status_code in (401, 403)
