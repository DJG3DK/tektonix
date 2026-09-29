"""The API balance card. It vanished twice: 2026-08-24, when the frontend
called the review service's path behind a login this app's users did not
have; and 2026-09-28, in the bundle, when the proxy to the review service
found no key file there. GET /api/router-balance asks OpenRouter itself,
with this deployment's own key, behind this app's own auth."""

from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User

_FAKE_USER = User(id=1, email="test@example.com", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

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
        _FakeAsyncClient.calls = getattr(_FakeAsyncClient, "calls", 0) + 1
        return _FakeResponse({"data": {"total_credits": 165, "total_usage": 143.4}})


def test_router_balance_asks_openrouter_with_this_deployments_key_behind_this_apps_own_auth(monkeypatch):
    import agent.model_config as mc

    monkeypatch.setattr(mc, "_openrouter_key", lambda: "sk-or-v1-thekey")
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _FAKE_USER)
    monkeypatch.setattr(srv.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(srv, "_balance_cache", {"data": None, "at": 0.0})
    _FakeAsyncClient.calls = 0
    client = TestClient(srv.app)

    res = client.get("/api/router-balance")

    assert res.status_code == 200
    assert res.json() == {"totalCredits": 165.0, "totalUsage": 143.4, "remaining": 165 - 143.4}
    assert _FakeAsyncClient.last_url == "https://openrouter.ai/api/v1/credits"
    assert _FakeAsyncClient.last_headers == {"Authorization": "Bearer sk-or-v1-thekey"}
    assert client.get("/api/router-balance").status_code == 200 and _FakeAsyncClient.calls == 1, "cached for a minute"


def _wired(monkeypatch, client_cls):
    import agent.model_config as mc

    monkeypatch.setattr(mc, "_openrouter_key", lambda: "sk-or-v1-thekey")
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _FAKE_USER)
    monkeypatch.setattr(srv.httpx, "AsyncClient", client_cls)
    monkeypatch.setattr(srv, "_balance_cache", {"data": None, "at": 0.0})
    return TestClient(srv.app)


def test_an_unreachable_openrouter_is_a_502_with_a_fixed_sentence(monkeypatch):
    """httpx errors were unhandled, so a network blip was a 500 with a
    traceback and the card showed nothing it could explain (2026-09-29)."""
    class _Down(_FakeAsyncClient):
        async def get(self, url, headers=None):
            raise srv.httpx.ConnectError("connection refused")

    res = _wired(monkeypatch, _Down).get("/api/router-balance")
    assert res.status_code == 502
    assert res.json()["detail"] == "could not reach OpenRouter for the credits request: ConnectError"
    assert "sk-or" not in res.text


def test_a_non_json_reply_is_a_502_not_a_500(monkeypatch):
    class _Garbage(_FakeAsyncClient):
        async def get(self, url, headers=None):
            r = _FakeResponse(None)
            r.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
            return r

    res = _wired(monkeypatch, _Garbage).get("/api/router-balance")
    assert res.status_code == 502 and "expected JSON" in res.json()["detail"]


def test_a_missing_key_does_not_ask_for_a_restart(monkeypatch):
    """The key is read on every call, so the sentence that told the operator
    to restart the agent sent them to do something that was not needed."""
    import agent.model_config as mc

    monkeypatch.setattr(mc, "_openrouter_key", lambda: None)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _FAKE_USER)
    monkeypatch.setattr(srv, "_balance_cache", {"data": None, "at": 0.0})
    res = TestClient(srv.app).get("/api/router-balance")
    assert res.status_code == 503 and "restart" not in res.json()["detail"]


def test_without_a_key_the_card_gets_a_reason_not_a_500(monkeypatch):
    import agent.model_config as mc

    monkeypatch.setattr(mc, "_openrouter_key", lambda: None)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _FAKE_USER)
    monkeypatch.setattr(srv, "_balance_cache", {"data": None, "at": 0.0})
    res = TestClient(srv.app).get("/api/router-balance")
    assert res.status_code == 503 and "OPENROUTER_API_KEY" in res.json()["detail"]


def test_router_balance_requires_login(monkeypatch):
    srv.app.dependency_overrides.pop(srv.require_full_auth, None)
    monkeypatch.setattr(srv.app.state, "auth_pool", None, raising=False)
    client = TestClient(srv.app)

    res = client.get("/api/router-balance")

    assert res.status_code in (401, 403)
