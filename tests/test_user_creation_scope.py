"""H7: creating a non-admin without an explicit repo list granted every repo.

CreateUserRequest.allowed_repos defaults to None, and User.can_access treats
None as unrestricted for ANY role — so `POST /api/auth/users {"role": "user"}`
minted an account with access to every project. The sibling PATCH endpoint
already rejected this; only the create path was fail-open.
"""

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User

def _row(uid, email, role, allowed_repos):
    """A complete agent_users row — _row_to_user reads every column."""
    return {"id": uid, "email": email, "role": role, "allowed_repos": allowed_repos,
            "totp_enabled": False, "must_change_password": True,
            "auto_approve_commands": False, "require_merge_review": True}


_ADMIN = User(id=1, email="admin@example.com", role="admin", allowed_repos=None,
              totp_enabled=True, must_change_password=False,
              auto_approve_commands=False, require_merge_review=True)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TEKTONIX_FEATURES", "multi-user")   # these are the licensed deployment's rules
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    return TestClient(srv.app)


def test_non_admin_without_a_repo_list_is_rejected(client):
    res = client.post("/api/auth/users", json={
        "email": "dev@example.com", "password": "CorrectHorse12", "role": "user"})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "allowed_repos" in detail and "every repo" in detail


def test_non_admin_with_an_explicit_empty_list_is_allowed(client, monkeypatch):
    """[] is a deliberate 'no repos yet' — it must stay expressible, otherwise
    the only way to create a user is to grant something."""
    created = {}

    async def no_existing(pool, email):
        return None

    async def fake_create(pool, email, password, role, allowed_repos, **kw):
        created.update(email=email, role=role, allowed_repos=allowed_repos)
        return _row(7, email, role, allowed_repos)

    import agent.auth as auth
    monkeypatch.setattr(auth, "get_user_by_email", no_existing)
    monkeypatch.setattr(auth, "create_user", fake_create)
    res = client.post("/api/auth/users", json={
        "email": "dev@example.com", "password": "CorrectHorse12",
        "role": "user", "allowed_repos": []})
    assert res.status_code in (200, 201), res.text
    assert created.get("allowed_repos") == []


def test_admins_may_still_omit_the_list(client, monkeypatch):
    """For an admin, unrestricted is the correct meaning, not a fail-open."""
    async def no_existing(pool, email):
        return None

    async def fake_create(pool, email, password, role, allowed_repos, **kw):
        return _row(8, email, role, allowed_repos)

    import agent.auth as auth
    monkeypatch.setattr(auth, "get_user_by_email", no_existing)
    monkeypatch.setattr(auth, "create_user", fake_create)
    res = client.post("/api/auth/users", json={
        "email": "boss@example.com", "password": "CorrectHorse12", "role": "admin"})
    assert res.status_code in (200, 201), res.text


def test_a_bad_auto_approve_scope_is_refused_before_the_account_exists(client, monkeypatch):
    """The scope check once ran after create_user: the admin saw a 400, the
    account existed with no audit line, and the retry returned 409
    (2026-09-29). Validation must come first."""
    import agent.auth as auth
    calls = []

    async def no_existing(pool, email):
        return None

    async def fake_create(pool, email, password, role, allowed_repos, **kw):
        calls.append(email)
        return _row(9, email, role, allowed_repos)

    monkeypatch.setattr(auth, "get_user_by_email", no_existing)
    monkeypatch.setattr(auth, "create_user", fake_create)
    # admin, auto mode on, no projects named -> the scope is empty
    res = client.post("/api/auth/users", json={
        "email": "boss@example.com", "password": "CorrectHorse12", "role": "admin",
        "auto_approve_commands": True})
    assert res.status_code == 400, res.text
    assert "auto_approve_repos" in res.json()["detail"]
    assert calls == [], "the account was created before its scope was validated"
    # ...and an unknown project in the scope is the same story
    res = client.post("/api/auth/users", json={
        "email": "boss@example.com", "password": "CorrectHorse12", "role": "admin",
        "auto_approve_commands": True, "auto_approve_repos": ["nope"]})
    assert res.status_code == 400, res.text
    assert "unknown project" in res.json()["detail"]
    assert calls == []


def test_a_valid_auto_approve_scope_is_applied_after_creation(client, monkeypatch):
    import agent.auth as auth
    from agent import audit
    from agent import config as agent_config
    monkeypatch.setattr(agent_config, "PROJECTS", {"sandbox": {}}, raising=False)
    applied = {}

    async def no_existing(pool, email):
        return None

    async def fake_create(pool, email, password, role, allowed_repos, **kw):
        return _row(10, email, role, allowed_repos)

    async def fake_auto(pool, uid, enabled, repos):
        applied.update(uid=uid, enabled=enabled, repos=repos)

    async def fake_audit(store, **kw):
        applied["audit"] = kw["action"]

    monkeypatch.setattr(auth, "get_user_by_email", no_existing)
    monkeypatch.setattr(auth, "create_user", fake_create)
    monkeypatch.setattr(auth, "update_auto_approve", fake_auto)
    monkeypatch.setattr(audit, "record", fake_audit)
    res = client.post("/api/auth/users", json={
        "email": "dev@example.com", "password": "CorrectHorse12", "role": "user",
        "allowed_repos": ["sandbox"], "auto_approve_commands": True})
    assert res.status_code == 201, res.text
    assert applied == {"uid": 10, "enabled": True, "repos": ["sandbox"], "audit": "settings.auto_approve"}
    assert res.json()["auto_approve_repos"] == ["sandbox"]
