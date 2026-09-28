"""The router balance is the operator's spend: admin-only.

It required only a session until 2026-09-23, so a restricted account could
read the operator's spend and remaining credit.
"""
import base64
import dataclasses
import secrets

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User


def _user(role):
    return User(id=2, email=f"{role}@example.com", role=role, allowed_repos=None if role == "admin" else ["p"],
                totp_enabled=True, must_change_password=False,
                auto_approve_commands=False, require_merge_review=True)


@pytest.fixture
def client(monkeypatch):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    fake = dataclasses.replace(srv.config, auth_secret_key=key)
    monkeypatch.setattr(srv, "config", fake)
    monkeypatch.setattr(srv.app.state, "config", fake)

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"total_credits": 100, "total_usage": 40}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **k):
            return _Resp()

    monkeypatch.setattr(srv.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(srv, "_balance_cache", {"data": None, "at": 0.0})
    import agent.model_config as mc
    monkeypatch.setattr(mc, "_openrouter_key", lambda: "sk-or-v1-x")
    return TestClient(srv.app)


def test_a_restricted_user_cannot_read_the_operators_spend(client, monkeypatch):
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _user("user"))
    assert client.get("/api/router-balance").status_code == 403


def test_an_admin_can(client, monkeypatch):
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _user("admin"))
    r = client.get("/api/router-balance")
    assert r.status_code == 200 and r.json()["remaining"] == 60
